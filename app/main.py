"""FFXIV Completion Tracker — FastAPI + HTMX.

Navigation mirrors the workbook: a menu tree in the sidebar, breadcrumb trail,
category-grid views for menu nodes and data-table views for content sheets.
Row state is toggled in place via HTMX; prerequisite chains are first-class.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

from app import db, lodestone_import, progress_io, progress_report, section_sort

RECONCILE_RUN_LOCK = threading.Lock()
LAST_RECONCILED_RUN_TOKEN: tuple[Any, ...] | None = None
SESSION_BASELINE_SNAPSHOT: dict[str, Any] | None = None
LAST_BETWEEN_RUN_REPORT_PATH: Path | None = None


def _load_session_baseline_snapshot(*, force: bool = False) -> dict[str, Any] | None:
    global SESSION_BASELINE_SNAPSHOT
    if force or SESSION_BASELINE_SNAPSHOT is None:
        SESSION_BASELINE_SNAPSHOT = progress_report.load_baseline_snapshot()
    return SESSION_BASELINE_SNAPSHOT


def _save_session_baseline_snapshot(
    conn,
    run_id: int,
    *,
    source: str,
    run_token: tuple[Any, ...] | None,
) -> Path:
    global SESSION_BASELINE_SNAPSHOT
    SESSION_BASELINE_SNAPSHOT = progress_report.build_snapshot(
        conn,
        run_id,
        source=source,
        run_token=run_token,
    )
    return progress_report.save_baseline_snapshot(SESSION_BASELINE_SNAPSHOT)


def _save_shutdown_progress_baseline() -> None:
    conn = db.get_connection()
    try:
        run_id, run_token = _latest_run_identity(conn)
        if run_id is None:
            return
        path = _save_session_baseline_snapshot(
            conn,
            run_id,
            source="project_close",
            run_token=run_token,
        )
        print(f"Progress baseline saved on shutdown: {path}")
    except Exception as exc:
        print(f"Progress baseline warning: could not save shutdown baseline: {exc}")
    finally:
        conn.close()


def _load_latest_progress_report() -> dict[str, Any] | None:
    return progress_report.load_latest_report()


def _progress_report_alert_for_character(character_id: int) -> dict[str, Any] | None:
    report_doc = _load_latest_progress_report()
    if not isinstance(report_doc, dict):
        return None

    unresolved = progress_report.count_unresolved_review_items(
        report_doc,
        character_id=character_id,
    )
    if unresolved <= 0:
        return None

    reason = str(report_doc.get("reason") or "progress-change")
    generated_at = str(report_doc.get("generated_at") or "")
    return {
        "unresolved": unresolved,
        "reason": reason,
        "generated_at": generated_at,
        "path": "/progress-reports",
    }


def _latest_run_identity(
    conn,
) -> tuple[int | None, tuple[Any, ...] | None]:
    row = conn.execute(
        """
        SELECT id, source_file, started_at, completed_at, sheet_count, row_count
        FROM ingest_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None, None

    run_id = int(row["id"])
    token: tuple[Any, ...] = (
        run_id,
        str(row["source_file"] or ""),
        str(row["started_at"] or ""),
        str(row["completed_at"] or ""),
        int(row["sheet_count"] or 0),
        int(row["row_count"] or 0),
    )
    return run_id, token


def _ensure_progress_reconciled_for_run(
    conn,
    run_id: int,
    run_token: tuple[Any, ...],
) -> None:
    """Reconcile sidecars for a run once per process lifetime.

    Handles in-process workbook ingests: when the latest ingest run identity
    changes, replay sidecars before request handlers read/write progress.
    """
    global LAST_RECONCILED_RUN_TOKEN

    if LAST_RECONCILED_RUN_TOKEN == run_token:
        return

    with RECONCILE_RUN_LOCK:
        global LAST_BETWEEN_RUN_REPORT_PATH
        if LAST_RECONCILED_RUN_TOKEN == run_token:
            return
        previous_run_token = LAST_RECONCILED_RUN_TOKEN
        report = progress_io.reconcile_all(conn, run_id)
        LAST_RECONCILED_RUN_TOKEN = run_token
        if report.characters:
            print(f"Progress reconcile (run {run_id}):")
            print(report.summary())
            if report.total_orphaned() > 0:
                print(
                    "Progress reconcile warning: "
                    f"{report.total_orphaned()} orphaned sidecar entries "
                    "were not replayed"
                )
            dropped_rows = sum(
                max(0, c.preexisting_db_rows - c.replayed_rows)
                for c in report.characters
            )
            if dropped_rows > 0:
                print(
                    "Progress reconcile warning: "
                    f"{dropped_rows} preexisting DB row(s) were replaced "
                    "by sidecar replay"
                )

        # First reconcile in a fresh process: keep any existing persisted
        # baseline (from the last close/ingest). If that baseline belongs to
        # a different run identity, generate a startup transition report.
        if previous_run_token is None:
            baseline = _load_session_baseline_snapshot()
            if baseline is None:
                path = _save_session_baseline_snapshot(
                    conn,
                    run_id,
                    source="session_start",
                    run_token=run_token,
                )
                print(
                    "Progress baseline initialized for between-run reports: "
                    f"{path}"
                )
                return

            baseline_token_raw = baseline.get("run_token") if isinstance(baseline, dict) else None
            baseline_token: tuple[Any, ...] | None = None
            if isinstance(baseline_token_raw, list):
                baseline_token = tuple(baseline_token_raw)

            baseline_differs = baseline_token is not None and baseline_token != run_token
            if baseline_token is None and isinstance(baseline, dict):
                baseline_run = baseline.get("run") if isinstance(baseline.get("run"), dict) else {}
                baseline_run_id = int(baseline_run.get("id") or 0) if baseline_run else 0
                baseline_source = str(baseline_run.get("source_file") or "") if baseline_run else ""
                baseline_differs = (
                    baseline_run_id != int(run_id)
                    or baseline_source != str(run_token[1])
                )

            if baseline_differs:
                orphaned_by_character = {
                    str(c.name): int(c.orphaned)
                    for c in report.characters
                    if int(c.orphaned) > 0
                }
                startup_doc, startup_path = progress_report.create_between_run_report(
                    conn,
                    run_id,
                    reason="startup-run-transition",
                    run_token=run_token,
                    baseline=baseline,
                    orphaned_by_character=orphaned_by_character,
                    persist=True,
                )
                if startup_path is not None:
                    LAST_BETWEEN_RUN_REPORT_PATH = startup_path
                    print(f"Progress startup transition report saved: {startup_path}")
                startup_summary = startup_doc.get("summary") if isinstance(startup_doc, dict) else None
                if isinstance(startup_summary, dict):
                    print(
                        "Progress startup transition summary: "
                        f"changed={startup_summary.get('characters_changed', 0)}, "
                        f"review_unresolved={startup_summary.get('review_unresolved', 0)}"
                    )

                rotated = _save_session_baseline_snapshot(
                    conn,
                    run_id,
                    source="startup_run",
                    run_token=run_token,
                )
                print(f"Progress baseline rotated after startup transition: {rotated}")
            return

        # In-process ingest/run transition: generate a transition report against
        # the previous baseline, then rotate baseline to the newly reconciled run.
        baseline = _load_session_baseline_snapshot()
        orphaned_by_character = {
            str(c.name): int(c.orphaned)
            for c in report.characters
            if int(c.orphaned) > 0
        }
        between_doc, between_path = progress_report.create_between_run_report(
            conn,
            run_id,
            reason="ingest-run-transition",
            run_token=run_token,
            baseline=baseline,
            orphaned_by_character=orphaned_by_character,
            persist=True,
        )
        if between_path is not None:
            LAST_BETWEEN_RUN_REPORT_PATH = between_path
            print(
                "Progress between-run report saved: "
                f"{between_path}"
            )
        summary = between_doc.get("summary") if isinstance(between_doc, dict) else None
        if isinstance(summary, dict) and summary.get("baseline_available"):
            print(
                "Progress between-run summary: "
                f"characters changed={summary.get('characters_changed', 0)}, "
                f"entries +{summary.get('entries_added', 0)} "
                f"-{summary.get('entries_removed', 0)} "
                f"~{summary.get('entries_changed', 0)}"
            )

        rotated = _save_session_baseline_snapshot(
            conn,
            run_id,
            source="ingest_run",
            run_token=run_token,
        )
        print(f"Progress baseline rotated after ingest run transition: {rotated}")


def reconcile_progress_sidecars() -> None:
    """Make the DB match the per-character JSON sidecars (the source of truth
    for progress). One-time bootstraps any character that has DB progress but
    no sidecar yet; otherwise replays each sidecar's entries onto the current
    run's character_progress table. Detail logic lives in progress_io."""
    conn = db.get_connection()
    try:
        global LAST_RECONCILED_RUN_TOKEN
        _load_session_baseline_snapshot()
        run_id, run_token = _latest_run_identity(conn)
        if run_id is None:
            LAST_RECONCILED_RUN_TOKEN = None
            print("Progress reconcile: skipped — no ingest run found "
                  "(run scripts/prep_xlsx_to_sqlite.py to populate the DB)")
            return
        assert run_token is not None
        _ensure_progress_reconciled_for_run(conn, run_id, run_token)
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    reconcile_progress_sidecars()
    try:
        yield
    finally:
        _save_shutdown_progress_baseline()


app = FastAPI(title="FFXIV Completion Tracker", lifespan=lifespan)

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

CHAR_COOKIE = "ffxiv_character"
LODESTONE_COOKIE = "lodestone_profile_url"
LODESTONE_BROWSER_COOKIE = "lodestone_cookie_source"
LODESTONE_STANDARD_COOKIE = "lodestone_include_standard"
LODESTONE_RUNS: dict[str, dict[str, Any]] = {}
LODESTONE_RUNS_LOCK = threading.Lock()
LODESTONE_OUTPUT_DIR = BASE.parent / "data" / "lodestone_probe"
LODESTONE_LOG_DIR = LODESTONE_OUTPUT_DIR / "logs"
CHAR_IMPORT_RUNS: dict[str, dict[str, Any]] = {}
CHAR_IMPORT_RUNS_LOCK = threading.Lock()
CHAR_IMPORT_LOG_DIR = LODESTONE_OUTPUT_DIR / "import_logs"
CHAR_IMPORT_UPLOAD_DIR = LODESTONE_OUTPUT_DIR / "import_uploads"
CHAR_IMPORT_UNMATCHED_DIR = LODESTONE_OUTPUT_DIR / "unmatched"
CHAR_IMPORT_HISTORY_DIR = LODESTONE_OUTPUT_DIR / "import_history"
MAX_PERSISTED_LOG_FILES_PER_TYPE = 10
THEME_COOKIE = "ffxiv_theme"
THEME_SCHEME_COOKIE = "ffxiv_theme_scheme"
THEME_ALLOWED_SCHEME_SETTINGS = {"default", "dark", "light"}
SECTION_SORT_COOKIE = "ffxiv_sheet_section_sort"
SECTION_SORT_OPTIONS = (
    {
        "value": section_sort.SORT_MODE_WORKBOOK,
        "label": section_sort.SORT_MODE_LABELS[section_sort.SORT_MODE_WORKBOOK],
    },
    {
        "value": section_sort.SORT_MODE_PROGRESSION,
        "label": section_sort.SORT_MODE_LABELS[section_sort.SORT_MODE_PROGRESSION],
    },
    {
        "value": section_sort.SORT_MODE_ENDGAME,
        "label": section_sort.SORT_MODE_LABELS[section_sort.SORT_MODE_ENDGAME],
    },
)
THEME_TOKEN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
THEME_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
THEME_REQUIRED_TOKENS = (
    "bg",
    "bg-soft",
    "panel",
    "panel-2",
    "panel-3",
    "line",
    "line-soft",
    "text",
    "muted",
    "faint",
    "accent",
    "accent-dk",
    "gold",
    "crystal-earth",
    "crystal-fire",
    "crystal-water",
    "crystal-wind",
    "done",
    "done-dk",
    "todo",
    "excluded",
    "danger",
)
THEME_REQUIRED_TOKEN_SET = set(THEME_REQUIRED_TOKENS)
THEMES_DIR = BASE / "themes"
THEME_CACHE_LOCK = threading.Lock()
THEME_CACHE: dict[str, Any] = {"signature": None, "catalog": None}
FF_THEME_NUMBER_RE = re.compile(
    r"(?:^|[^a-z0-9])ff\s*0*(\d{1,2})(?:[^0-9]|$)",
    re.IGNORECASE,
)
POKEMON_THEME_NAME_RE = re.compile(
    r"pokemon[-_\s]*(red|blue|yellow|gold|silver|crystal|ruby|sapphire|emerald|diamond|pearl|platinum)",
    re.IGNORECASE,
)
POKEMON_THEME_ORDER: dict[str, tuple[int, int]] = {
    "red": (1, 1),
    "blue": (1, 2),
    "yellow": (1, 3),
    "gold": (2, 1),
    "silver": (2, 2),
    "crystal": (2, 3),
    "ruby": (3, 1),
    "sapphire": (3, 2),
    "emerald": (3, 3),
    "diamond": (4, 1),
    "pearl": (4, 2),
    "platinum": (4, 3),
}

BUILTIN_THEME_DARK = {
    "bg": "#0b0e15",
    "bg-soft": "#10151f",
    "panel": "#161c28",
    "panel-2": "#1b2230",
    "panel-3": "#212a3b",
    "line": "#2a3445",
    "line-soft": "#222b3a",
    "text": "#e7ecf5",
    "muted": "#8995a8",
    "faint": "#5d6a7e",
    "accent": "#6ba4e8",
    "accent-dk": "#3f6fae",
    "gold": "#e2bd72",
    "crystal-earth": "#45c78a",
    "crystal-fire": "#d96b6b",
    "crystal-water": "#6ba4e8",
    "crystal-wind": "#e2bd72",
    "done": "#45c78a",
    "done-dk": "#2f8c61",
    "todo": "#586374",
    "excluded": "#4a4f5c",
    "danger": "#d96b6b",
}

BUILTIN_THEME_LIGHT = {
    "bg": "#f2f3f5",
    "bg-soft": "#e6e8ed",
    "panel": "#fefefe",
    "panel-2": "#f6f7f8",
    "panel-3": "#edeff1",
    "line": "#3d5b8f",
    "line-soft": "#9faec6",
    "text": "#1c2c3f",
    "muted": "#425a76",
    "faint": "#6c829d",
    "accent": "#1f6bc7",
    "accent-dk": "#275591",
    "gold": "#b8851e",
    "crystal-earth": "#36ba7c",
    "crystal-fire": "#bf3131",
    "crystal-water": "#2070cf",
    "crystal-wind": "#c79329",
    "done": "#2e9e69",
    "done-dk": "#277c55",
    "todo": "#657285",
    "excluded": "#969cab",
    "danger": "#ba2c2c",
}


def cookie_theme_id(request: Request) -> str:
    return (request.cookies.get(THEME_COOKIE) or "").strip()


def set_theme_cookie(response, theme_id: str) -> None:
    response.set_cookie(
        THEME_COOKIE,
        theme_id.strip(),
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )


def cookie_theme_scheme(request: Request) -> str:
    raw = (request.cookies.get(THEME_SCHEME_COOKIE) or "default").strip().lower()
    return raw if raw in THEME_ALLOWED_SCHEME_SETTINGS else "default"


def set_theme_scheme_cookie(response, scheme: str) -> None:
    value = normalize_theme_scheme_setting(scheme)
    response.set_cookie(
        THEME_SCHEME_COOKIE,
        value,
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )


def normalize_theme_scheme_setting(raw: str) -> str:
    value = (raw or "").strip().lower()
    return value if value in THEME_ALLOWED_SCHEME_SETTINGS else "default"


def cookie_section_sort_mode(request: Request) -> str:
    raw = request.cookies.get(SECTION_SORT_COOKIE)
    return section_sort.normalize_sort_mode(raw)


def set_section_sort_cookie(response, mode: str) -> None:
    value = section_sort.normalize_sort_mode(mode)
    response.set_cookie(
        SECTION_SORT_COOKIE,
        value,
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
    )


def _extract_theme_color(raw: object) -> str | None:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        value = raw.get("value")
        if isinstance(value, str):
            return value.strip()
    return None


def _flatten_theme_tokens(raw_colors: object) -> dict[str, str]:
    tokens: dict[str, str] = {}
    if not isinstance(raw_colors, dict):
        return tokens

    for key, value in raw_colors.items():
        direct = _extract_theme_color(value)
        if direct is not None:
            tokens[str(key)] = direct
            continue

        if not isinstance(value, dict):
            continue
        for token, entry in value.items():
            color = _extract_theme_color(entry)
            if color is not None:
                tokens[str(token)] = color

    return tokens


def _looks_like_scheme_map(raw_colors: dict[str, Any]) -> bool:
    if not raw_colors:
        return False
    keys = {str(key).lower() for key in raw_colors}
    if not keys.issubset({"dark", "light"}):
        return False
    return all(isinstance(value, dict) for value in raw_colors.values())


def _extract_theme_schemes(raw_doc: dict[str, Any]) -> dict[str, dict[str, str]]:
    schemes: dict[str, dict[str, str]] = {}

    raw_colors = raw_doc.get("colors")
    if isinstance(raw_colors, dict):
        if _looks_like_scheme_map(raw_colors):
            for scheme_name, block in raw_colors.items():
                tokens = _flatten_theme_tokens(block)
                if tokens:
                    schemes[str(scheme_name).lower()] = tokens
        else:
            tokens = _flatten_theme_tokens(raw_colors)
            if tokens:
                schemes["dark"] = tokens

    raw_colors_light = raw_doc.get("colorsLight")
    if isinstance(raw_colors_light, dict):
        tokens = _flatten_theme_tokens(raw_colors_light)
        if tokens:
            schemes["light"] = tokens

    raw_schemes = raw_doc.get("schemes")
    if isinstance(raw_schemes, dict):
        for scheme_name, scheme_block in raw_schemes.items():
            if not isinstance(scheme_block, dict):
                continue
            color_block = scheme_block.get("colors", scheme_block)
            tokens = _flatten_theme_tokens(color_block)
            if tokens:
                schemes[str(scheme_name).lower()] = tokens

    return schemes


def _validate_theme(path: Path, raw_doc: dict[str, Any]) -> dict[str, Any]:
    meta = raw_doc.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    theme_id = str(meta.get("id") or path.stem).strip() or path.stem
    name = str(meta.get("name") or theme_id).strip() or theme_id
    schemes = _extract_theme_schemes(raw_doc)
    default_scheme = str(meta.get("defaultScheme") or "dark").strip().lower()
    errors: list[str] = []
    warnings: list[str] = []

    for required_scheme in ("dark", "light"):
        if required_scheme not in schemes:
            errors.append(f"Missing required {required_scheme} scheme.")

    for scheme_name, tokens in schemes.items():
        missing = sorted(THEME_REQUIRED_TOKEN_SET.difference(tokens.keys()))
        if missing:
            errors.append(
                f"{scheme_name}: missing required tokens: {', '.join(missing)}"
            )

        for token, value in tokens.items():
            if not THEME_TOKEN_NAME_RE.fullmatch(token):
                errors.append(f"{scheme_name}: invalid token name '{token}'")
                continue
            if token in THEME_REQUIRED_TOKEN_SET and not THEME_HEX_RE.fullmatch(value):
                errors.append(f"{scheme_name}: token '{token}' has invalid hex value '{value}'")

    light_tokens = schemes.get("light")
    if isinstance(light_tokens, dict):
        light_surfaces = [
            light_tokens.get("bg"),
            light_tokens.get("bg-soft"),
            light_tokens.get("panel"),
            light_tokens.get("panel-2"),
            light_tokens.get("panel-3"),
        ]
        unique_surfaces = {value for value in light_surfaces if value}
        if len(unique_surfaces) <= 2:
            warnings.append(
                "light scheme surfaces are nearly flat (bg/bg-soft/panel/panel-2/panel-3)."
            )
        if light_tokens.get("muted") == light_tokens.get("faint"):
            warnings.append("light scheme text ramp collapsed: muted equals faint.")
        if light_tokens.get("todo") == light_tokens.get("excluded"):
            warnings.append("light scheme state ramp collapsed: todo equals excluded.")

    if default_scheme not in schemes:
        warnings.append(
            f"defaultScheme '{default_scheme}' is unavailable; falling back to dark/first available scheme."
        )
        if "dark" in schemes:
            default_scheme = "dark"
        elif schemes:
            default_scheme = next(iter(schemes))
        else:
            default_scheme = "dark"

    return {
        "id": theme_id,
        "name": name,
        "file_name": path.name,
        "path": str(path),
        "default_scheme": default_scheme,
        "schemes": schemes,
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
    }


def _theme_signature(theme_paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    for path in theme_paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((path.name, int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(signature)


def _builtin_theme_entry() -> dict[str, Any]:
    return {
        "id": "builtin-aetherial",
        "name": "Builtin Aetherial",
        "file_name": "<builtin>",
        "path": "<builtin>",
        "default_scheme": "dark",
        "schemes": {
            "dark": dict(BUILTIN_THEME_DARK),
            "light": dict(BUILTIN_THEME_LIGHT),
        },
        "errors": [],
        "warnings": ["Using builtin fallback theme because no valid theme files were found."],
        "valid": True,
    }


def _extract_ff_theme_number(*values: object) -> int | None:
    for value in values:
        if value is None:
            continue
        match = FF_THEME_NUMBER_RE.search(str(value))
        if match is None:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            continue
    return None


def _extract_pokemon_theme_order(*values: object) -> tuple[int, int, str] | None:
    for value in values:
        if value is None:
            continue
        match = POKEMON_THEME_NAME_RE.search(str(value))
        if match is None:
            continue
        token = match.group(1).lower()
        order = POKEMON_THEME_ORDER.get(token)
        if order is not None:
            return order[0], order[1], token
    return None


def _theme_sort_key(theme: dict[str, Any]) -> tuple[int, int, str, str]:
    theme_id = str(theme.get("id") or "")
    file_name = str(theme.get("file_name") or "")
    file_stem = Path(file_name).stem if file_name and file_name != "<builtin>" else ""
    name = str(theme.get("name") or file_stem or theme_id)

    ff_number = _extract_ff_theme_number(theme_id, file_stem, name)
    if ff_number is not None:
        variant = (theme_id or file_stem or name).lower()
        return (0, ff_number, variant, name.lower())

    pokemon_order = _extract_pokemon_theme_order(theme_id, file_stem, name)
    if pokemon_order is not None:
        generation, position, token = pokemon_order
        return (1, generation * 10 + position, token, name.lower())

    if file_stem.lower() == "template":
        return (3, 9999, file_stem.lower(), name.lower())

    label = (theme_id or file_stem or name).lower()
    return (2, 9999, label, name.lower())


def get_theme_catalog() -> dict[str, Any]:
    theme_paths = sorted(path for path in THEMES_DIR.glob("*.json") if path.is_file())
    signature = _theme_signature(theme_paths)
    with THEME_CACHE_LOCK:
        cached_signature = THEME_CACHE.get("signature")
        cached_catalog = THEME_CACHE.get("catalog")
    if signature == cached_signature and isinstance(cached_catalog, dict):
        return cached_catalog

    themes: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []

    for path in theme_paths:
        try:
            raw_doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            invalid.append({
                "id": path.stem,
                "name": path.stem,
                "file_name": path.name,
                "path": str(path),
                "default_scheme": "dark",
                "schemes": {},
                "errors": [f"Could not parse JSON: {exc}"],
                "warnings": [],
                "valid": False,
            })
            continue

        if not isinstance(raw_doc, dict):
            invalid.append({
                "id": path.stem,
                "name": path.stem,
                "file_name": path.name,
                "path": str(path),
                "default_scheme": "dark",
                "schemes": {},
                "errors": ["Theme root must be a JSON object."],
                "warnings": [],
                "valid": False,
            })
            continue

        entry = _validate_theme(path, raw_doc)
        if entry["valid"]:
            themes.append(entry)
        else:
            invalid.append(entry)

    themes.sort(key=_theme_sort_key)
    if not themes:
        themes.append(_builtin_theme_entry())

    preferred_default = next(
        (
            theme
            for theme in themes
            if str(theme.get("file_name") or "").lower() == "aetherial-dark.json"
        ),
        None,
    )
    if preferred_default is None:
        preferred_default = next(
            (theme for theme in themes if str(theme.get("id")) == "aetherial-dark"),
            themes[0],
        )
    catalog = {
        "themes": themes,
        "invalid": invalid,
        "default_theme_id": str(preferred_default.get("id") or themes[0]["id"]),
    }
    with THEME_CACHE_LOCK:
        THEME_CACHE["signature"] = signature
        THEME_CACHE["catalog"] = catalog
    return catalog


def _render_theme_css(tokens: dict[str, str]) -> str:
    ordered_tokens = [token for token in THEME_REQUIRED_TOKENS if token in tokens]
    extra_tokens = sorted(
        token
        for token in tokens.keys()
        if token not in THEME_REQUIRED_TOKEN_SET and THEME_TOKEN_NAME_RE.fullmatch(token)
    )
    lines = [":root {"]
    for token in ordered_tokens + extra_tokens:
        lines.append(f"  --{token}: {tokens[token]};")
    lines.append("}")
    return "\n".join(lines)


def resolve_theme_state(request: Request) -> dict[str, Any]:
    catalog = get_theme_catalog()
    themes = catalog["themes"]
    by_id = {str(theme.get("id")): theme for theme in themes}

    requested_theme_id = cookie_theme_id(request)
    theme = by_id.get(requested_theme_id)
    if theme is None:
        default_theme_id = str(catalog.get("default_theme_id") or "")
        theme = by_id.get(default_theme_id) or themes[0]

    scheme_setting = normalize_theme_scheme_setting(cookie_theme_scheme(request))
    if scheme_setting == "default":
        effective_scheme = str(theme.get("default_scheme") or "dark")
    else:
        effective_scheme = scheme_setting

    schemes = theme.get("schemes") if isinstance(theme.get("schemes"), dict) else {}
    if effective_scheme not in schemes:
        effective_scheme = "dark" if "dark" in schemes else next(iter(schemes), "dark")

    active_tokens = schemes.get(effective_scheme)
    if not isinstance(active_tokens, dict):
        active_tokens = dict(BUILTIN_THEME_DARK)

    return {
        "catalog": catalog,
        "theme": theme,
        "theme_id": str(theme.get("id") or ""),
        "scheme_setting": scheme_setting,
        "effective_scheme": effective_scheme,
        "theme_css": _render_theme_css(active_tokens),
        "first_paint_bg": active_tokens.get("bg", BUILTIN_THEME_DARK["bg"]),
        "first_paint_text": active_tokens.get("text", BUILTIN_THEME_DARK["text"]),
        "color_scheme_meta": effective_scheme,
    }


# --- request context --------------------------------------------------------


# --- request context --------------------------------------------------------

def cookie_character_id(request: Request) -> int | None:
    raw = request.cookies.get(CHAR_COOKIE)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def set_char_cookie(response, character_id: int) -> None:
    response.set_cookie(
        CHAR_COOKIE, str(character_id), max_age=60 * 60 * 24 * 365, samesite="lax"
    )


def cookie_lodestone_url(request: Request) -> str:
    return (request.cookies.get(LODESTONE_COOKIE) or "").strip()


def set_lodestone_cookie(response, lodestone_url: str) -> None:
    response.set_cookie(
        LODESTONE_COOKIE, lodestone_url.strip(),
        max_age=60 * 60 * 24 * 365, samesite="lax"
    )


def cookie_lodestone_browser(request: Request) -> str:
    value = (request.cookies.get(LODESTONE_BROWSER_COOKIE) or "edge").strip().lower()
    return value if value in {"edge", "chrome", "firefox"} else "edge"


def set_lodestone_browser_cookie(response, browser: str) -> None:
    response.set_cookie(
        LODESTONE_BROWSER_COOKIE, browser, max_age=60 * 60 * 24 * 365, samesite="lax"
    )


def cookie_lodestone_include_standard(request: Request) -> bool:
    return (request.cookies.get(LODESTONE_STANDARD_COOKIE) or "1") == "1"


def set_lodestone_include_standard_cookie(response, enabled: bool) -> None:
    response.set_cookie(
        LODESTONE_STANDARD_COOKIE, "1" if enabled else "0",
        max_age=60 * 60 * 24 * 365, samesite="lax"
    )


def normalize_lodestone_url(raw_url: str) -> str | None:
    value = raw_url.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc.endswith("finalfantasyxiv.com"):
        return None
    if not parsed.path.startswith("/lodestone/"):
        return None
    return value


def normalize_cookie_source(raw: str) -> str | None:
    value = raw.strip().lower()
    return value if value in {"edge", "chrome", "firefox"} else None


def normalize_import_source(raw: str) -> str | None:
    value = raw.strip().lower()
    return value if value in {"lodestone-json", "desktop-app"} else None


def normalize_import_input_method(raw: str) -> str | None:
    value = raw.strip().lower()
    return value if value in {"upload-file", "server-file", "auto"} else None


def normalize_lodestone_level_mode(raw: str) -> str | None:
    value = raw.strip().lower().replace("_", "-")
    return value if value in {"keep-highest", "overwrite"} else None


def parse_extra_paths(raw: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for token in raw.replace(",", "\n").splitlines():
        normalized = token.strip().strip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return values


def resolve_payload_path(raw_path: str) -> Path | None:
    value = raw_path.strip().strip('"')
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path

    workspace_path = BASE.parent / path
    if workspace_path.exists():
        return workspace_path

    # If a bare filename is provided, prefer known payload dirs.
    if path.parent == Path('.'):
        probe_candidate = LODESTONE_OUTPUT_DIR / path.name
        if probe_candidate.exists():
            return probe_candidate
        upload_candidate = CHAR_IMPORT_UPLOAD_DIR / path.name
        if upload_candidate.exists():
            return upload_candidate

    return workspace_path


def _safe_upload_name(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "").strip())
    cleaned = cleaned.strip("._") or "payload.json"
    if not cleaned.lower().endswith(".json"):
        cleaned += ".json"
    return cleaned


def save_uploaded_payload(upload: UploadFile) -> Path:
    CHAR_IMPORT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_upload_name(upload.filename or "payload.json")
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = CHAR_IMPORT_UPLOAD_DIR / f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_name}"
    data = upload.file.read()
    out_path.write_bytes(data)
    return out_path


def run_lodestone_authenticated_probe(
    lodestone_url: str,
    cookie_source: str,
    include_standard: bool,
    extra_paths_raw: str,
    progress: Callable[[str], None] | None = None,
) -> Path:
    from CharacterScraping import lodestone_probe as probe

    def log(message: str) -> None:
        if progress is not None:
            progress(message)

    extra_paths: list[str] = []
    if include_standard:
        extra_paths.extend(probe.STANDARD_AUTH_PAGES.values())
    for path in parse_extra_paths(extra_paths_raw):
        if path not in extra_paths:
            extra_paths.append(path)

    log(f"Importing Lodestone cookies from {cookie_source}")
    session = probe.session_from_installed_browser_cookies(cookie_source, progress=log)
    try:
        log("Collecting profile, class/job, minions, mounts, and achievements")
        payload = probe.collect_character(lodestone_url, session=session, progress=log)
        log(f"Collecting authenticated pages ({len(extra_paths)} paths)")
        payload["authenticated_pages"] = probe.collect_authenticated_pages(
            lodestone_url, session, extra_paths, progress=log
        )
        payload["auth"] = {
            "method": "installed_browser_cookies",
            "source_browser": cookie_source,
            "extra_paths": extra_paths,
        }
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = LODESTONE_OUTPUT_DIR / f"{probe.character_output_stem(payload)}_auth_{timestamp}.json"
        log(f"Saving output to {output_path}")
        probe.save_payload(payload, output_path)
        _prune_files_by_pattern(LODESTONE_OUTPUT_DIR, pattern="*_auth_*.json")
        log("Scrape completed")
        return output_path
    finally:
        session.close()


def _append_lodestone_log_file(log_path: Path, line: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        # Keep runtime logging resilient even if local file writes fail.
        pass


def _path_mtime_sort_key(path: Path) -> tuple[int, str]:
    try:
        return path.stat().st_mtime_ns, path.name.casefold()
    except OSError:
        return 0, path.name.casefold()


def _prune_files_by_pattern(
    directory: Path,
    *,
    pattern: str,
    keep: int = MAX_PERSISTED_LOG_FILES_PER_TYPE,
) -> None:
    if keep < 1:
        return
    try:
        files = [path for path in directory.glob(pattern) if path.is_file()]
    except OSError:
        return
    files.sort(key=_path_mtime_sort_key, reverse=True)
    for stale_path in files[keep:]:
        try:
            stale_path.unlink()
        except OSError:
            # Best-effort retention cleanup should never break normal flow.
            continue


def _update_lodestone_run(run_id: str, **updates: Any) -> None:
    with LODESTONE_RUNS_LOCK:
        existing = LODESTONE_RUNS.get(run_id)
        if not existing:
            return
        existing.update(updates)


def _append_lodestone_run_log(run_id: str, message: str) -> None:
    line = ""
    log_path: Path | None = None
    with LODESTONE_RUNS_LOCK:
        run = LODESTONE_RUNS.get(run_id)
        if not run:
            return
        logs = run.setdefault("logs", [])
        ts = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        logs.append(line)
        if len(logs) > 600:
            del logs[: len(logs) - 600]
        log_path_value = run.get("log_path")
        if isinstance(log_path_value, str) and log_path_value:
            log_path = Path(log_path_value)

    if log_path is not None and line:
        _append_lodestone_log_file(log_path, line)


def _run_lodestone_probe_job(
    run_id: str,
    lodestone_url: str,
    cookie_source: str,
    include_standard_pages: bool,
    extra_paths: str,
) -> None:
    _update_lodestone_run(run_id, status="running", started_at=dt.datetime.now().isoformat())
    _append_lodestone_run_log(run_id, "Job started")
    try:
        output_path = run_lodestone_authenticated_probe(
            lodestone_url=lodestone_url,
            cookie_source=cookie_source,
            include_standard=include_standard_pages,
            extra_paths_raw=extra_paths,
            progress=lambda msg: _append_lodestone_run_log(run_id, msg),
        )
        _update_lodestone_run(
            run_id,
            status="completed",
            saved_path=str(output_path),
            finished_at=dt.datetime.now().isoformat(),
        )
        _append_lodestone_run_log(run_id, "Job completed")
    except Exception as exc:
        _update_lodestone_run(
            run_id,
            status="failed",
            error=str(exc).strip() or exc.__class__.__name__,
            finished_at=dt.datetime.now().isoformat(),
        )
        _append_lodestone_run_log(run_id, f"Job failed: {exc}")
        _append_lodestone_run_log(run_id, traceback.format_exc())


def _update_character_import_run(run_id: str, **updates: Any) -> None:
    with CHAR_IMPORT_RUNS_LOCK:
        existing = CHAR_IMPORT_RUNS.get(run_id)
        if not existing:
            return
        existing.update(updates)


def _append_character_import_run_log(run_id: str, message: str) -> None:
    line = ""
    log_path: Path | None = None
    with CHAR_IMPORT_RUNS_LOCK:
        run = CHAR_IMPORT_RUNS.get(run_id)
        if not run:
            return
        logs = run.setdefault("logs", [])
        ts = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        logs.append(line)
        if len(logs) > 800:
            del logs[: len(logs) - 800]
        log_path_value = run.get("log_path")
        if isinstance(log_path_value, str) and log_path_value:
            log_path = Path(log_path_value)

    if log_path is not None and line:
        _append_lodestone_log_file(log_path, line)


def _active_character_import_run(character_id: int) -> dict[str, Any] | None:
    with CHAR_IMPORT_RUNS_LOCK:
        for run in CHAR_IMPORT_RUNS.values():
            if int(run.get("character_id") or -1) != int(character_id):
                continue
            status = str(run.get("status") or "").lower()
            if status in {"queued", "running"}:
                return {
                    "id": str(run.get("id") or ""),
                    "status": status,
                }
    return None


def _ensure_character_import_idle(character_id: int) -> None:
    active = _active_character_import_run(character_id)
    if active is None:
        return
    run_id = str(active.get("id") or "").strip()
    status = str(active.get("status") or "running")
    suffix = f" (run_id={run_id})" if run_id else ""
    raise HTTPException(
        409,
        f"This character currently has an import {status}{suffix}. Wait for it to finish before editing progress.",
    )


def _row_snapshot(
    conn,
    *,
    run_id: int,
    character_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None,
) -> dict[str, Any] | None:
    row = db.fetch_row(
        conn,
        run_id,
        character_id,
        sheet_name,
        row_index,
        starting_class,
    )
    if row is None or row.get("row_type") == "section":
        return None
    pct_raw = row.get("progress_percent")
    pct = float(pct_raw) if isinstance(pct_raw, (int, float)) else None
    explicit_state_raw = row.get("explicit_state")
    explicit_state = str(explicit_state_raw) if explicit_state_raw is not None else None
    return {
        "sheet_name": str(sheet_name),
        "row_index": int(row_index),
        "row_type": str(row.get("row_type") or "checkbox"),
        "state": str(row.get("eff") or "todo"),
        "progress_percent": pct,
        "explicit": explicit_state is not None,
        "explicit_state": explicit_state,
    }


def _row_snapshots(
    conn,
    *,
    run_id: int,
    character_id: int,
    sheet_name: str,
    row_indices: list[int],
    starting_class: str | None,
) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for idx in sorted(set(int(i) for i in row_indices)):
        snap = _row_snapshot(
            conn,
            run_id=run_id,
            character_id=character_id,
            sheet_name=sheet_name,
            row_index=idx,
            starting_class=starting_class,
        )
        if snap is not None:
            out[idx] = snap
    return out


def _snapshot_diff_changes(
    before: dict[int, dict[str, Any]],
    after: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for idx in sorted(set(before.keys()) | set(after.keys())):
        b = before.get(idx)
        a = after.get(idx)
        if b is None or a is None:
            continue
        same_state = b.get("state") == a.get("state")
        same_pct = b.get("progress_percent") == a.get("progress_percent")
        same_explicit = bool(b.get("explicit")) == bool(a.get("explicit"))
        if same_state and same_pct and same_explicit:
            continue
        changes.append({
            "sheet_name": str(a.get("sheet_name") or b.get("sheet_name") or ""),
            "row_index": int(idx),
            "row_type": str(a.get("row_type") or b.get("row_type") or "checkbox"),
            "before": {
                "state": str(b.get("state") or "todo"),
                "progress_percent": b.get("progress_percent"),
                "explicit": bool(b.get("explicit")),
            },
            "after": {
                "state": str(a.get("state") or "todo"),
                "progress_percent": a.get("progress_percent"),
                "explicit": bool(a.get("explicit")),
            },
        })
    return changes


def _history_step(
    *,
    action: str,
    changes: list[dict[str, Any]],
    kind: str = "row-change",
    run_id: int | None = None,
    character_id: int | None = None,
) -> dict[str, Any] | None:
    if not changes:
        return None
    payload: dict[str, Any] = {
        "kind": kind,
        "action": action,
        "created_at": dt.datetime.now().isoformat(),
        "changes": changes,
    }
    if run_id is not None:
        payload["run_id"] = int(run_id)
    if character_id is not None:
        payload["character_id"] = int(character_id)
    return payload


def _normalize_history_snapshot(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    state = str(raw.get("state") or "todo")
    pct_raw = raw.get("progress_percent")
    pct = float(pct_raw) if isinstance(pct_raw, (int, float)) else None
    snap: dict[str, Any] = {
        "state": state,
        "progress_percent": pct,
    }
    if "explicit" in raw:
        snap["explicit"] = bool(raw.get("explicit"))
    return snap


def _history_snapshot_matches(expected: dict[str, Any], current: dict[str, Any] | None) -> bool:
    if current is None:
        return False
    if str(expected.get("state") or "todo") != str(current.get("state") or "todo"):
        return False
    if expected.get("progress_percent") != current.get("progress_percent"):
        return False
    if "explicit" in expected and bool(expected.get("explicit")) != bool(current.get("explicit")):
        return False
    return True


def _set_hx_triggers(resp: HTMLResponse, history_step: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"progress-changed": True}
    if history_step is not None:
        payload["history-step"] = history_step
    resp.headers["HX-Trigger"] = json.dumps(payload)


def _rows_to_complete_for_history(
    conn,
    *,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None,
) -> list[int]:
    impacted: list[int] = []
    seen: set[int] = set()
    stack = [int(row_index)]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if db.effective_state(
            conn,
            character_id,
            run_id,
            sheet_name,
            current,
            starting_class,
        ) != "done":
            impacted.append(current)
        prereqs = conn.execute(
            """
            SELECT source_row_index FROM edges
            WHERE run_id = ? AND sheet_name = ? AND edge_type = 'sequence'
              AND target_row_index = ? AND source_row_index IS NOT NULL
            """,
            (run_id, sheet_name, current),
        ).fetchall()
        for pr in prereqs:
            stack.append(int(pr["source_row_index"]))
    return impacted


def _rows_to_revert_for_history(
    conn,
    *,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    new_state: str,
    starting_class: str | None,
) -> list[int]:
    impacted: list[int] = []
    seen: set[int] = set()
    root = int(row_index)
    stack = [root]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)

        cur_state = db.effective_state(
            conn,
            character_id,
            run_id,
            sheet_name,
            current,
            starting_class,
        )
        if current == root:
            if cur_state != new_state:
                impacted.append(current)
        elif cur_state == "done":
            impacted.append(current)

        successors = conn.execute(
            """
            SELECT target_row_index FROM edges
            WHERE run_id = ? AND sheet_name = ? AND edge_type = 'sequence'
              AND source_row_index = ? AND target_row_index IS NOT NULL
            """,
            (run_id, sheet_name, current),
        ).fetchall()
        for nxt in successors:
            stack.append(int(nxt["target_row_index"]))
    return impacted


def _write_import_history(run_id: str, payload: dict[str, Any]) -> Path | None:
    try:
        CHAR_IMPORT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = CHAR_IMPORT_HISTORY_DIR / f"{run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _prune_files_by_pattern(CHAR_IMPORT_HISTORY_DIR, pattern="*.json")
        return path
    except OSError:
        return None


def _character_existing_progress_counts(
    conn,
    *,
    run_id: int,
    character_id: int,
    starting_class: str | None,
) -> dict[str, int]:
    progress_rows_raw = conn.execute(
        """
        SELECT COUNT(*)
        FROM character_progress
        WHERE run_id = ? AND character_id = ?
        """,
        (run_id, character_id),
    ).fetchone()
    progress_rows = int(progress_rows_raw[0] if progress_rows_raw else 0)

    class_overrides = 0
    normalized_starting_class = str(starting_class or "").strip().upper()
    if normalized_starting_class:
        class_overrides_raw = conn.execute(
            """
            SELECT COUNT(*)
            FROM class_overrides
            WHERE run_id = ? AND starting_class = ?
            """,
            (run_id, normalized_starting_class),
        ).fetchone()
        class_overrides = int(class_overrides_raw[0] if class_overrides_raw else 0)

    return {
        "row_overrides": progress_rows,
        "class_overrides": class_overrides,
        "total": progress_rows + class_overrides,
    }


def _run_character_import_job(
    run_id: str,
    *,
    import_type: str,
    character_id: int,
    source_path: Path,
    clear_existing: bool,
    lodestone_level_mode: str,
) -> None:
    _update_character_import_run(
        run_id,
        status="running",
        started_at=dt.datetime.now().isoformat(),
    )
    import_label = "Desktop import" if import_type == "desktop-app" else "Lodestone JSON import"
    _append_character_import_run_log(run_id, f"{import_label} job started")
    conn = db.get_connection()
    try:
        global LAST_BETWEEN_RUN_REPORT_PATH
        run_id_db = db.latest_run_id(conn)
        if run_id_db is None:
            raise ValueError("No ingest run found. Run scripts/prep_xlsx_to_sqlite.py first.")
        character = db.get_character(conn, character_id)
        if character is None:
            raise ValueError(f"Character id {character_id} was not found")
        starting_class = character["starting_class"]

        _, run_token_before = _latest_run_identity(conn)
        compare_baseline_snapshot = progress_report.build_snapshot(
            conn,
            run_id_db,
            source=f"pre-{import_type}-import",
            run_token=run_token_before,
        )

        before_snapshot = db.snapshot_trackable_rows(
            conn,
            run_id_db,
            character_id,
            starting_class,
        )
        existing_progress_counts = _character_existing_progress_counts(
            conn,
            run_id=run_id_db,
            character_id=character_id,
            starting_class=starting_class,
        )
        had_existing_progress = existing_progress_counts["total"] > 0
        _append_character_import_run_log(
            run_id,
            "Existing explicit progress before import: "
            f"{existing_progress_counts['total']} "
            f"(rows={existing_progress_counts['row_overrides']}, "
            f"class_overrides={existing_progress_counts['class_overrides']})",
        )

        if import_type == "desktop-app":
            summary = lodestone_import.import_desktop_completion(
                conn,
                character_id=character_id,
                completion_path=source_path,
                clear_existing=clear_existing,
                progress=lambda msg: _append_character_import_run_log(run_id, msg),
            )
        else:
            summary = lodestone_import.import_lodestone_payload(
                conn,
                character_id=character_id,
                payload_path=source_path,
                clear_existing=clear_existing,
                level_merge_mode=lodestone_level_mode,
                progress=lambda msg: _append_character_import_run_log(run_id, msg),
            )

        after_snapshot = db.snapshot_trackable_rows(
            conn,
            run_id_db,
            character_id,
            starting_class,
        )

        import_changes: list[dict[str, Any]] = []
        for key, after_item in after_snapshot.items():
            before_item = before_snapshot.get(key)
            if before_item is None:
                continue
            same_state = before_item.get("state") == after_item.get("state")
            same_pct = before_item.get("progress_percent") == after_item.get("progress_percent")
            same_explicit = bool(before_item.get("explicit")) == bool(after_item.get("explicit"))
            if same_state and same_pct and same_explicit:
                continue
            import_changes.append({
                "sheet_name": str(after_item.get("sheet_name") or before_item.get("sheet_name") or ""),
                "row_index": int(after_item.get("row_index") or before_item.get("row_index") or 0),
                "row_type": str(after_item.get("row_type") or before_item.get("row_type") or "checkbox"),
                "before": {
                    "state": str(before_item.get("state") or "todo"),
                    "progress_percent": before_item.get("progress_percent"),
                    "explicit": bool(before_item.get("explicit")),
                },
                "after": {
                    "state": str(after_item.get("state") or "todo"),
                    "progress_percent": after_item.get("progress_percent"),
                    "explicit": bool(after_item.get("explicit")),
                },
            })

        history_step_summary: dict[str, Any] | None = None
        history_path: Path | None = None
        between_run_report_path: Path | None = None

        if import_changes:
            history_payload = {
                "kind": "import-run",
                "import_run_id": run_id,
                "run_id": run_id_db,
                "character_id": character_id,
                "created_at": dt.datetime.now().isoformat(),
                "changes": import_changes,
            }
            history_path = _write_import_history(run_id, history_payload)
            if history_path is not None:
                _append_character_import_run_log(
                    run_id,
                    f"Saved import history to {history_path}",
                )
            history_step_summary = {
                "kind": "import-run",
                "action": import_label,
                "import_run_id": run_id,
                "run_id": run_id_db,
                "character_id": character_id,
                "changes_count": len(import_changes),
            }

        _, run_token_after = _latest_run_identity(conn)
        if clear_existing:
            baseline_path = _save_session_baseline_snapshot(
                conn,
                run_id_db,
                source=f"{import_type}-clean-run",
                run_token=run_token_after,
            )
            _append_character_import_run_log(
                run_id,
                f"Clean run checked: skipped diff report and reset baseline to {baseline_path}",
            )
        elif not had_existing_progress:
            baseline_path = _save_session_baseline_snapshot(
                conn,
                run_id_db,
                source=f"{import_type}-initial-import",
                run_token=run_token_after,
            )
            _append_character_import_run_log(
                run_id,
                "Initial import detected (no prior explicit progress): "
                f"skipped diff report and reset baseline to {baseline_path}",
            )
        elif import_type == "desktop-app":
            baseline_path = _save_session_baseline_snapshot(
                conn,
                run_id_db,
                source="desktop-app-import-no-report",
                run_token=run_token_after,
            )
            _append_character_import_run_log(
                run_id,
                "Desktop import: skipped progress report audit generation and "
                f"reset baseline to {baseline_path}",
            )
        else:
            between_doc, between_path = progress_report.create_between_run_report(
                conn,
                run_id_db,
                reason=f"{import_type}-import-transition",
                run_token=run_token_after,
                baseline=compare_baseline_snapshot,
                persist=True,
            )
            if between_path is not None:
                between_run_report_path = between_path
                LAST_BETWEEN_RUN_REPORT_PATH = between_path
                _append_character_import_run_log(
                    run_id,
                    f"Saved between-run report to {between_path}",
                )
            between_summary = between_doc.get("summary") if isinstance(between_doc, dict) else None
            if isinstance(between_summary, dict):
                _append_character_import_run_log(
                    run_id,
                    "Report summary: "
                    f"changed={between_summary.get('characters_changed', 0)}, "
                    f"review_unresolved={between_summary.get('review_unresolved', 0)}",
                )

            baseline_path = _save_session_baseline_snapshot(
                conn,
                run_id_db,
                source=f"{import_type}-import",
                run_token=run_token_after,
            )
            _append_character_import_run_log(
                run_id,
                f"Rotated baseline snapshot to {baseline_path}",
            )

        unmatched_report_path = _write_unmatched_report(run_id, summary)
        _update_character_import_run(
            run_id,
            status="completed",
            finished_at=dt.datetime.now().isoformat(),
            unmatched_report_path=(str(unmatched_report_path) if unmatched_report_path else None),
            history_path=(str(history_path) if history_path else None),
            progress_report_path=(str(between_run_report_path) if between_run_report_path else None),
            summary={
                "character_id": summary.character_id,
                "character_name": summary.character_name,
                "source_path": summary.source_path,
                "run_id": summary.run_id,
                "total_candidates": summary.total_candidates,
                "matched_candidates": summary.matched_candidates,
                "unmatched_candidates": summary.unmatched_candidates,
                "rows_applied": summary.rows_applied,
                "rows_skipped_already_done": summary.rows_skipped_already_done,
                "existing_progress_before_import": existing_progress_counts,
                "unmatched_sample": summary.unmatched_items[:20],
                "history_step": history_step_summary,
            },
        )
        _append_character_import_run_log(run_id, f"{import_label} job completed")
    except Exception as exc:
        _update_character_import_run(
            run_id,
            status="failed",
            error=str(exc).strip() or exc.__class__.__name__,
            finished_at=dt.datetime.now().isoformat(),
        )
        _append_character_import_run_log(run_id, f"{import_label} job failed: {exc}")
        _append_character_import_run_log(run_id, lodestone_import.format_exception(exc))
    finally:
        conn.close()


def _write_unmatched_report(
    run_id: str,
    summary: lodestone_import.ImportSummary,
) -> Path | None:
    unmatched_report_path: Path | None = None
    if not summary.unmatched_items:
        return None
    try:
        CHAR_IMPORT_UNMATCHED_DIR.mkdir(parents=True, exist_ok=True)
        unmatched_report_path = CHAR_IMPORT_UNMATCHED_DIR / f"{run_id}.json"
        unmatched_payload = {
            "run_id": run_id,
            "character_id": summary.character_id,
            "character_name": summary.character_name,
            "source_path": summary.source_path,
            "created_at": dt.datetime.now().isoformat(),
            "total_candidates": summary.total_candidates,
            "matched_candidates": summary.matched_candidates,
            "unmatched_candidates": summary.unmatched_candidates,
            "items": summary.unmatched_items,
        }
        unmatched_report_path.write_text(
            json.dumps(unmatched_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _prune_files_by_pattern(CHAR_IMPORT_UNMATCHED_DIR, pattern="*.json")
        _append_character_import_run_log(
            run_id,
            f"Saved unmatched report to {unmatched_report_path}",
        )
    except Exception as exc:
        _append_character_import_run_log(
            run_id,
            f"Could not save unmatched report: {exc}",
        )
    return unmatched_report_path


class Ctx:
    """Per-request context.

    `full=True` (default) loads the nav tree, rollups, and character list — what
    a page render needs. `full=False` is the lightweight path for HTMX partial
    endpoints; it skips the expensive sheet/rollup/tree work that a single-row
    write or fragment doesn't need.
    """

    def __init__(self, request: Request, *, full: bool = True):
        self.request = request
        self.conn = db.get_connection()
        run_id, run_token = _latest_run_identity(self.conn)
        if run_id is None:
            raise HTTPException(503, "No ingest run found. Run the prep script first.")
        assert run_token is not None
        self.run_id: int = run_id
        _ensure_progress_reconciled_for_run(self.conn, self.run_id, run_token)
        self.theme_state = resolve_theme_state(request)
        self.character = db.resolve_active_character(
            self.conn, cookie_character_id(request)
        )
        self.character_id = int(self.character["id"])
        self.section_sort_mode = cookie_section_sort_mode(request)
        self.starting_class: str | None = (
            self.character["starting_class"]
            if "starting_class" in self.character.keys() else None
        )
        self.full = full
        if full:
            self.characters = db.fetch_characters(self.conn)
            self.sheets = db.fetch_all_sheets(self.conn, self.run_id)
            self.rollups = db.sheet_rollups(
                self.conn, self.run_id, self.character_id, self.starting_class
            )
            self.tree, self.overall, self.sheets_by_name = db.build_nav_tree(
                self.sheets, self.rollups
            )
            db.attach_content_virtual_nodes(
                self.conn,
                self.tree,
                self.sheets_by_name,
                self.run_id,
                self.character_id,
                self.starting_class,
            )

    def close(self) -> None:
        self.conn.close()

    def require_content_sheet(self, sheet_name: str) -> dict:
        """Lightweight single-sheet lookup for partial endpoints."""
        if self.full:
            sheet = self.sheets_by_name.get(sheet_name)
        else:
            row = db.fetch_sheet(self.conn, self.run_id, sheet_name)
            if row is None and db.VIRTUAL_SEP in sheet_name:
                source_sheet = sheet_name.split(db.VIRTUAL_SEP, 1)[0]
                row = db.fetch_sheet(self.conn, self.run_id, source_sheet)
            sheet = dict(row) if row is not None else None
        if sheet is None:
            raise HTTPException(404, f"Unknown sheet: {sheet_name}")
        if sheet["is_menu"]:
            raise HTTPException(400, "Not a content sheet")
        return sheet

    def base_context(self) -> dict:
        avatar_path = BASE / "static" / "avatars" / f"{self.character_id}.jpg"
        theme = self.theme_state
        active_theme = theme["theme"] if isinstance(theme.get("theme"), dict) else {}
        return {
            "request": self.request,
            "characters": self.characters,
            "character": self.character,
            "tree": self.tree,
            "overall": self.overall,
            "overall_pct": db.pct(self.overall),
            "avatar_url": f"/static/avatars/{self.character_id}.jpg" if avatar_path.exists() else None,
            "theme_id": theme["theme_id"],
            "theme_name": str(active_theme.get("name") or theme["theme_id"]),
            "theme_css": theme["theme_css"],
            "theme_scheme_setting": theme["scheme_setting"],
            "theme_effective_scheme": theme["effective_scheme"],
            "section_sort_mode": self.section_sort_mode,
            "section_sort_mode_label": section_sort.sort_mode_label(
                self.section_sort_mode
            ),
            "progress_report_alert": _progress_report_alert_for_character(self.character_id),
            "theme_first_paint_bg": theme["first_paint_bg"],
            "theme_first_paint_text": theme["first_paint_text"],
            "theme_color_scheme_meta": theme["color_scheme_meta"],
        }

    def header_context(self, sheet_name: str | None) -> dict:
        node = db.find_node(self.tree, sheet_name) if sheet_name else None
        roll = self.rollups.get(sheet_name) if sheet_name else None
        if roll is None and node is not None:
            node_roll = node.get("roll")
            if isinstance(node_roll, dict):
                roll = node_roll
        return {
            "overall": self.overall,
            "overall_pct": db.pct(self.overall),
            "cur_node": node,
            "cur_roll": roll,
            "cur_pct": db.pct(roll) if roll else 0.0,
            "cur_sheet": sheet_name,
        }

    def render(self, template: str, extra: dict) -> HTMLResponse:
        active = extra.get("active_sheet")
        db.mark_active_path(self.tree, active)
        ctx = {**self.base_context(), **self.header_context(active), **extra}
        resp = templates.TemplateResponse(self.request, template, ctx)
        set_char_cookie(resp, self.character_id)
        set_theme_cookie(resp, self.theme_state["theme_id"])
        set_theme_scheme_cookie(resp, self.theme_state["scheme_setting"])
        set_section_sort_cookie(resp, self.section_sort_mode)
        return resp


# --- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    ctx = Ctx(request)
    try:
        # top-level menu cards + a few "needs attention" chains
        cards = []
        for node in ctx.tree:
            cards.append({
                "sheet_name": node["sheet_name"],
                "title": node["title"],
                "roll": node["roll"],
                "pct": node["pct"],
                "children": len(node["children"]),
            })
        chains = db.chain_sheets_overview(
            ctx.conn, ctx.run_id, ctx.character_id, ctx.starting_class
        )[:6]
        return ctx.render("dashboard.html", {
            "cards": cards,
            "chains": chains,
            "active_sheet": None,
        })
    finally:
        ctx.close()


@app.get("/browse/{sheet_name}", response_class=HTMLResponse)
def browse(request: Request, sheet_name: str, q: str = "", state: str = "all"):
    ctx = Ctx(request)
    try:
        sheet = ctx.sheets_by_name.get(sheet_name)
        # Backward compatibility for earlier virtual-node URLs that used
        # "::" as the section separator before db.VIRTUAL_SEP was finalized.
        if sheet is None and "::" in sheet_name:
            sheet_name = sheet_name.replace("::", db.VIRTUAL_SEP)
            sheet = ctx.sheets_by_name.get(sheet_name)
        if sheet is None:
            raise HTTPException(404, f"Unknown sheet: {sheet_name}")

        crumbs = db.breadcrumb_path(ctx.sheets_by_name, sheet_name)
        node = db.find_node(ctx.tree, sheet_name)

        if sheet["is_menu"]:
            # category-grid view: child cards with aggregated progress,
            # grouped by parent_menu_section (the workbook column header
            # the child came from). Sections appear in workbook order;
            # any child without a section goes to a final "Other" bucket.
            # Group child cards by parent_menu_section (the workbook column
            # they came from). Renamed from "items" to "cards" so Jinja's
            # attribute lookup doesn't collide with the dict ``.items()``
            # method when iterating ``group.cards`` in the template.
            section_order: list[str | None] = []
            section_cards: dict[str | None, list[dict]] = {}
            for child in (node["children"] if node else []):
                card = {
                    "sheet_name": child["sheet_name"],
                    "title": child["title"],
                    "is_menu": child["is_menu"],
                    "roll": child["roll"],
                    "pct": child["pct"],
                    "children": len(child["children"]),
                }
                section = child.get("parent_menu_section")
                if section not in section_cards:
                    section_cards[section] = []
                    section_order.append(section)
                section_cards[section].append(card)

            sections = [
                {"label": s, "cards": section_cards[s]}
                for s in section_order if s is not None
            ]
            # children with no detected section render last under a soft heading
            if None in section_cards:
                sections.append({"label": None, "cards": section_cards[None]})
            children_flat = [c for s in sections for c in s["cards"]]
            return ctx.render("menu.html", {
                "sheet": sheet,
                "crumbs": crumbs,
                "sections": sections,
                "children": children_flat,  # kept for the page-sub count
                "node": node,
                "active_sheet": sheet_name,
            })

        # content sheet with synthesized subpages: render a link-only index.
        # This keeps parent pages like Grand Company Ranks focused on routing
        # into their child tracks instead of mixing all rows in one long table.
        if node and node.get("children"):
            section_order: list[str | None] = []
            section_cards: dict[str | None, list[dict]] = {}
            for child in node["children"]:
                card = {
                    "sheet_name": child["sheet_name"],
                    "title": child["title"],
                    "is_menu": child["is_menu"],
                    "roll": child["roll"],
                    "pct": child["pct"],
                    "children": len(child["children"]),
                }
                section = child.get("parent_menu_section")
                if section not in section_cards:
                    section_cards[section] = []
                    section_order.append(section)
                section_cards[section].append(card)

            sections = [
                {"label": s, "cards": section_cards[s]}
                for s in section_order if s is not None
            ]
            if None in section_cards:
                sections.append({"label": None, "cards": section_cards[None]})
            children_flat = [c for s in sections for c in s["cards"]]
            return ctx.render("menu.html", {
                "sheet": sheet,
                "crumbs": crumbs,
                "sections": sections,
                "children": children_flat,
                "node": node,
                "active_sheet": sheet_name,
            })

        # content sheet: data-table view grouped by section
        source_sheet_name = str(sheet.get("source_sheet") or sheet_name)
        source_sheet = ctx.sheets_by_name.get(source_sheet_name)
        if source_sheet is None:
            raise HTTPException(404, f"Unknown source sheet: {source_sheet_name}")

        rows = db.fetch_rows(
            ctx.conn, ctx.run_id, ctx.character_id, source_sheet_name,
            q=q, state=state, starting_class=ctx.starting_class,
        )
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, source_sheet_name,
            ctx.starting_class,
        )
        for row in rows:
            row["chain_info"] = flags.get(row["row_index"])
        groups = db.group_rows_by_section(
            rows,
            sheet_name=source_sheet_name,
            section_sort_mode=ctx.section_sort_mode,
        )

        if sheet.get("is_virtual") and sheet.get("virtual_kind") == "content_group":
            allowed_row_indexes = {
                int(i)
                for i in (sheet.get("row_indexes") or [])
                if isinstance(i, int) or (isinstance(i, str) and i.isdigit())
            }
            allowed_section_rows = {
                int(i)
                for i in (sheet.get("section_row_indexes") or [])
                if isinstance(i, int) or (isinstance(i, str) and i.isdigit())
            }
            if allowed_row_indexes:
                filtered_groups: list[dict] = []
                for group in groups:
                    rows_for_group = [
                        row
                        for row in group.get("rows", [])
                        if int(row.get("row_index") or -1) in allowed_row_indexes
                    ]
                    if rows_for_group:
                        filtered_groups.append({
                            **group,
                            "rows": rows_for_group,
                        })
                groups = filtered_groups
            elif allowed_section_rows:
                groups = [
                    g for g in groups
                    if int(g.get("row_index") or -1) in allowed_section_rows
                ]
            else:
                label_prefixes = [
                    " ".join(str(p).strip().lower().split())
                    for p in (sheet.get("row_label_prefixes") or [])
                    if str(p).strip()
                ]
                if label_prefixes:
                    filtered_groups: list[dict] = []
                    for group in groups:
                        rows_for_group = []
                        for row in group.get("rows", []):
                            label_norm = " ".join(str(row.get("label") or "").strip().lower().split())
                            if any(label_norm.startswith(f"{prefix} ") for prefix in label_prefixes):
                                rows_for_group.append(row)
                        if rows_for_group:
                            filtered_groups.append({
                                **group,
                                "rows": rows_for_group,
                            })
                    groups = filtered_groups

        shown = sum(len(g["rows"]) for g in groups)
        view_sheet = dict(source_sheet)
        view_sheet["sheet_name"] = source_sheet_name
        if sheet.get("is_virtual") and sheet.get("virtual_kind") == "content_group":
            view_sheet["title"] = sheet["title"]
            view_sheet["total_rows"] = shown

        roll = (
            node.get("roll")
            if node is not None and sheet.get("is_virtual")
            else ctx.rollups.get(source_sheet_name)
        )
        if not isinstance(roll, dict):
            roll = db._empty_roll()

        import json as _json
        return ctx.render("sheet.html", {
            "sheet": view_sheet,
            "columns": _json.loads(source_sheet["data_columns_json"]),
            "crumbs": crumbs,
            "groups": groups,
            "roll": roll,
            "pct": db.pct(roll),
            "q": q,
            "state": state,
            "shown": shown,
            "sheet_total_rows": int(view_sheet.get("total_rows") or shown),
            "browse_sheet_name": sheet_name,
            "section_sort_supported": db.sheet_supports_section_sort(source_sheet_name),
            "section_sort_mode": ctx.section_sort_mode,
            "section_sort_mode_label": section_sort.sort_mode_label(
                ctx.section_sort_mode
            ),
            "active_sheet": sheet_name,
        })
    finally:
        ctx.close()


@app.get("/chains", response_class=HTMLResponse)
def chains_overview(request: Request):
    ctx = Ctx(request)
    try:
        chains = db.chain_sheets_overview(
            ctx.conn, ctx.run_id, ctx.character_id, ctx.starting_class
        )
        return ctx.render("chains.html", {"chains": chains, "active_sheet": None})
    finally:
        ctx.close()


@app.get("/characters", response_class=HTMLResponse)
def characters_page(
    request: Request,
    error: str = "",
    run_id: str = "",
    payload_path: str = "",
    desktop_path: str = "",
    import_source: str = "",
    import_input: str = "",
    lodestone_level_mode: str = "",
):
    ctx = Ctx(request)
    try:
        run = None
        if run_id:
            with CHAR_IMPORT_RUNS_LOCK:
                run = CHAR_IMPORT_RUNS.get(run_id)

        run_import_source = ""
        run_import_input = ""
        run_level_mode = ""
        if isinstance(run, dict):
            run_import_source = str(run.get("import_type") or "").strip().lower()
            run_import_input = str(run.get("input_method") or "").strip().lower()
            run_level_mode = str(run.get("lodestone_level_mode") or "").strip().lower()

        selected_import_source = normalize_import_source(import_source) or normalize_import_source(run_import_source) or "lodestone-json"
        selected_import_input_method = (
            normalize_import_input_method(import_input)
            or normalize_import_input_method(run_import_input)
            or "upload-file"
        )
        if selected_import_input_method == "auto":
            selected_import_input_method = "upload-file"

        selected_lodestone_level_mode = (
            normalize_lodestone_level_mode(lodestone_level_mode)
            or normalize_lodestone_level_mode(run_level_mode)
            or "keep-highest"
        )

        suggested_payload = ""
        suggested_desktop_path = ""
        incoming_path = payload_path.strip()
        if incoming_path:
            if selected_import_source == "desktop-app":
                suggested_desktop_path = incoming_path
            else:
                suggested_payload = incoming_path

        if desktop_path.strip():
            suggested_desktop_path = desktop_path.strip()

        latest_payloads = sorted(
            LODESTONE_OUTPUT_DIR.glob("*_auth_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        payload_options = [str(p) for p in latest_payloads[:40]]
        if not suggested_payload and payload_options:
            suggested_payload = payload_options[0]

        desktop_options = [str(p) for p in lodestone_import.list_detected_completion_files(limit=30)]
        if not suggested_desktop_path and desktop_options:
            suggested_desktop_path = desktop_options[0]

        suggested_source_path = suggested_payload if selected_import_source == "lodestone-json" else suggested_desktop_path

        return ctx.render("characters.html", {
            "error": error,
            "starting_classes": db.STARTING_CLASSES,
            "import_run_id": run_id,
            "import_run": run,
            "suggested_payload": suggested_payload,
            "payload_options": payload_options,
            "suggested_desktop_path": suggested_desktop_path,
            "desktop_completion_options": desktop_options,
            "selected_import_source": selected_import_source,
            "selected_import_input_method": selected_import_input_method,
            "selected_lodestone_level_mode": selected_lodestone_level_mode,
            "suggested_source_path": suggested_source_path,
            "active_sheet": None,
        })
    finally:
        ctx.close()


def _submit_character_import(
    *,
    import_source: str,
    import_input_method: str,
    character_id: int,
    payload_path: str,
    payload_file: UploadFile | None,
    clear_existing: str,
    lodestone_level_mode: str,
) -> RedirectResponse:
    normalized_source = normalize_import_source(import_source)
    if normalized_source is None:
        return RedirectResponse(
            f"/characters?error={quote('Choose a valid import source.')}",
            status_code=303,
        )
    normalized_input_method = normalize_import_input_method(import_input_method)
    if normalized_input_method is None:
        return RedirectResponse(
            f"/characters?error={quote('Choose a valid import input method.')}",
            status_code=303,
        )
    normalized_level_mode = normalize_lodestone_level_mode(lodestone_level_mode)
    if normalized_level_mode is None:
        return RedirectResponse(
            f"/characters?error={quote('Choose a valid Lodestone level merge mode.')}",
            status_code=303,
        )

    resolved_path: Path | None = None

    def _resolve_uploaded_file() -> Path | None:
        nonlocal payload_file
        if payload_file is not None and payload_file.filename:
            try:
                return save_uploaded_payload(payload_file)
            except OSError as exc:
                label = "desktop completion file" if normalized_source == "desktop-app" else "payload"
                raise ValueError(f"Could not save uploaded {label}: {exc}") from exc
        return None

    def _resolve_server_file() -> Path | None:
        nonlocal payload_path
        path = resolve_payload_path(payload_path)
        if path is None and normalized_source == "desktop-app":
            detected = lodestone_import.list_detected_completion_files(limit=1)
            if detected:
                return detected[0]
        return path

    try:
        if normalized_input_method == "upload-file":
            resolved_path = _resolve_uploaded_file()
        elif normalized_input_method == "server-file":
            resolved_path = _resolve_server_file()
        else:
            # Backward-compatible precedence for legacy endpoints.
            resolved_path = _resolve_uploaded_file() or _resolve_server_file()
    except ValueError as exc:
        return RedirectResponse(
            f"/characters?error={quote(str(exc))}&import_source={quote(normalized_source)}&import_input={quote(normalized_input_method)}&lodestone_level_mode={quote(normalized_level_mode)}",
            status_code=303,
        )

    if resolved_path is None:
        if normalized_input_method == "upload-file":
            message = (
                "Choose a desktop completion JSON file in the file picker."
                if normalized_source == "desktop-app"
                else "Choose a JSON payload file in the file picker."
            )
        elif normalized_input_method == "server-file":
            message = (
                "Choose a detected desktop completion path."
                if normalized_source == "desktop-app"
                else "Choose a server payload from data/lodestone_probe."
            )
        else:
            message = (
                "Choose a desktop completion JSON file or pick a detected completion path."
                if normalized_source == "desktop-app"
                else "Choose a JSON payload file or pick a server payload below."
            )
        return RedirectResponse(
            f"/characters?error={quote(message)}&import_source={quote(normalized_source)}&import_input={quote(normalized_input_method)}&lodestone_level_mode={quote(normalized_level_mode)}",
            status_code=303,
        )

    if not resolved_path.exists() or not resolved_path.is_file():
        label = "Desktop completion file" if normalized_source == "desktop-app" else "Payload file"
        return RedirectResponse(
            f"/characters?error={quote(f'{label} not found: {resolved_path}')}&import_source={quote(normalized_source)}&import_input={quote(normalized_input_method)}&lodestone_level_mode={quote(normalized_level_mode)}",
            status_code=303,
        )

    conn = db.get_connection()
    try:
        char = db.get_character(conn, character_id)
    finally:
        conn.close()
    if char is None:
        return RedirectResponse(
            f"/characters?error={quote(f'Character id {character_id} was not found')}&import_source={quote(normalized_source)}&import_input={quote(normalized_input_method)}&lodestone_level_mode={quote(normalized_level_mode)}",
            status_code=303,
        )

    active_run = _active_character_import_run(character_id)
    if active_run is not None:
        run_ref = str(active_run.get("id") or "").strip()
        detail = f" (run_id={run_ref})" if run_ref else ""
        return RedirectResponse(
            f"/characters?error={quote(f'An import is already in progress for this character{detail}. Wait for it to finish.')}&import_source={quote(normalized_source)}&import_input={quote(normalized_input_method)}&lodestone_level_mode={quote(normalized_level_mode)}",
            status_code=303,
        )

    run_id = uuid.uuid4().hex
    log_path = CHAR_IMPORT_LOG_DIR / f"{run_id}.log"
    clear_existing_flag = clear_existing == "1"
    import_label = "Desktop import" if normalized_source == "desktop-app" else "Lodestone import"
    _append_lodestone_log_file(
        log_path,
        f"[{dt.datetime.now().strftime('%H:%M:%S')}] {import_label} created for character_id={character_id} source={resolved_path}",
    )
    _prune_files_by_pattern(CHAR_IMPORT_LOG_DIR, pattern="*.log")
    with CHAR_IMPORT_RUNS_LOCK:
        CHAR_IMPORT_RUNS[run_id] = {
            "id": run_id,
            "import_type": normalized_source,
            "input_method": normalized_input_method,
            "status": "queued",
            "character_id": character_id,
            "character_name": char["name"],
            "payload_path": str(resolved_path),
            "clear_existing": clear_existing_flag,
            "lodestone_level_mode": normalized_level_mode,
            "log_path": str(log_path),
            "logs": [f"[{dt.datetime.now().strftime('%H:%M:%S')}] {import_label} queued"],
        }

    worker = threading.Thread(
        target=_run_character_import_job,
        kwargs={
            "run_id": run_id,
            "import_type": normalized_source,
            "character_id": character_id,
            "source_path": resolved_path,
            "clear_existing": clear_existing_flag,
            "lodestone_level_mode": normalized_level_mode,
        },
        daemon=True,
    )
    worker.start()

    return RedirectResponse(
        f"/characters?run_id={quote(run_id)}&import_source={quote(normalized_source)}&import_input={quote(normalized_input_method)}&payload_path={quote(str(resolved_path))}&lodestone_level_mode={quote(normalized_level_mode)}",
        status_code=303,
    )


@app.post("/characters/import")
def character_import(
    character_id: int = Form(...),
    import_source: str = Form("lodestone-json"),
    import_input_method: str = Form("upload-file"),
    payload_path: str = Form(""),
    payload_file: UploadFile | None = File(None),
    clear_existing: str = Form("0"),
    lodestone_level_mode: str = Form("keep-highest"),
):
    return _submit_character_import(
        import_source=import_source,
        import_input_method=import_input_method,
        character_id=character_id,
        payload_path=payload_path,
        payload_file=payload_file,
        clear_existing=clear_existing,
        lodestone_level_mode=lodestone_level_mode,
    )


@app.post("/characters/import-lodestone")
def character_import_lodestone(
    character_id: int = Form(...),
    payload_path: str = Form(""),
    payload_file: UploadFile | None = File(None),
    clear_existing: str = Form("0"),
    lodestone_level_mode: str = Form("keep-highest"),
):
    return _submit_character_import(
        import_source="lodestone-json",
        import_input_method="auto",
        character_id=character_id,
        payload_path=payload_path,
        payload_file=payload_file,
        clear_existing=clear_existing,
        lodestone_level_mode=lodestone_level_mode,
    )


@app.post("/characters/import-desktop-app")
def character_import_desktop_app(
    character_id: int = Form(...),
    completion_path: str = Form(""),
    completion_file: UploadFile | None = File(None),
    clear_existing: str = Form("0"),
    lodestone_level_mode: str = Form("keep-highest"),
):
    return _submit_character_import(
        import_source="desktop-app",
        import_input_method="auto",
        character_id=character_id,
        payload_path=completion_path,
        payload_file=completion_file,
        clear_existing=clear_existing,
        lodestone_level_mode=lodestone_level_mode,
    )


@app.get("/characters/import-status")
def character_import_status(run_id: str):
    with CHAR_IMPORT_RUNS_LOCK:
        run = CHAR_IMPORT_RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown run id")
        logs = run.get("logs") or []
        summary = run.get("summary") or {}
        payload = {
            "id": run.get("id"),
            "import_type": run.get("import_type"),
            "status": run.get("status"),
            "error": run.get("error"),
            "character_id": run.get("character_id"),
            "character_name": run.get("character_name"),
            "payload_path": run.get("payload_path"),
            "clear_existing": run.get("clear_existing"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "log_path": run.get("log_path"),
            "unmatched_report_path": run.get("unmatched_report_path"),
            "progress_report_path": run.get("progress_report_path"),
            "progress_report_url": (
                "/progress-reports"
                if run.get("progress_report_path") else None
            ),
            "unmatched_report_url": (
                f"/characters/import-unmatched?run_id={quote(str(run.get('id') or ''))}"
                if run.get("unmatched_report_path") else None
            ),
            "summary": summary,
            "log_tail": "\n".join(logs[-160:]),
        }
    return JSONResponse(payload)


@app.get("/characters/import-history-step")
def character_import_history_step(run_id: str):
    with CHAR_IMPORT_RUNS_LOCK:
        run = CHAR_IMPORT_RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown run id")
        history_path_raw = run.get("history_path")
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}

    history_path: Path | None
    if history_path_raw:
        history_path = Path(str(history_path_raw))
    else:
        history_path = CHAR_IMPORT_HISTORY_DIR / f"{run_id}.json"

    if not history_path.exists() or not history_path.is_file():
        raise HTTPException(404, "No history payload for this run")

    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Could not read history payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(500, "History payload malformed")

    action = "Imported progress"
    history_summary = summary.get("history_step") if isinstance(summary, dict) else None
    if isinstance(history_summary, dict):
        action = str(history_summary.get("action") or action)

    response_step = {
        "kind": "import-run",
        "action": action,
        "created_at": str(payload.get("created_at") or dt.datetime.now().isoformat()),
        "import_run_id": run_id,
        "run_id": payload.get("run_id"),
        "character_id": payload.get("character_id"),
        "changes": payload.get("changes") if isinstance(payload.get("changes"), list) else [],
    }
    return JSONResponse(response_step)


@app.get("/characters/import-unmatched", response_class=HTMLResponse)
def character_import_unmatched_page(run_id: str):
    with CHAR_IMPORT_RUNS_LOCK:
        run = CHAR_IMPORT_RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown run id")
        report_path_raw = run.get("unmatched_report_path")
        character_name = str(run.get("character_name") or "Character")

    if not report_path_raw:
        return HTMLResponse("<h1>No unmatched report for this run.</h1>", status_code=200)

    report_path = Path(str(report_path_raw))
    if not report_path.exists() or not report_path.is_file():
        raise HTTPException(404, "Unmatched report file not found")

    try:
        doc = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "Could not read unmatched report")

    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        items = []

    rows: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "unknown")
        label = str(item.get("label") or "")
        reason = str(item.get("reason") or "")
        rows.append(
            f"<tr><td>{bucket}</td><td>{label}</td><td>{reason}</td></tr>"
        )

    body = "\n".join(rows) if rows else "<tr><td colspan='3'>No unmatched items.</td></tr>"
    html = f"""
<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1.0' />
  <title>Unmatched Items - {character_name}</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 20px; background: #0f1218; color: #e7ecf5; }}
    a {{ color: #8ec2ff; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 1100px; background: #171d28; }}
    th, td {{ border: 1px solid #2c3647; padding: 8px 10px; text-align: left; }}
    th {{ background: #202a3b; }}
    .meta {{ color: #a8b2c3; margin-bottom: 12px; }}
  </style>
</head>
<body>
  <h1>Unmatched Items</h1>
  <div class='meta'>Run ID: {run_id} | Character: {character_name} | Count: {len(rows)}</div>
  <p><a href='/characters/import-unmatched.json?run_id={quote(run_id)}'>Download JSON report</a></p>
  <table>
        <thead><tr><th>Category</th><th>Label</th><th>Reason</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</body>
</html>
"""
    return HTMLResponse(html)


@app.get("/characters/import-unmatched.json")
def character_import_unmatched_json(run_id: str):
    with CHAR_IMPORT_RUNS_LOCK:
        run = CHAR_IMPORT_RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown run id")
        report_path_raw = run.get("unmatched_report_path")

    if not report_path_raw:
        raise HTTPException(404, "No unmatched report for this run")

    report_path = Path(str(report_path_raw))
    if not report_path.exists() or not report_path.is_file():
        raise HTTPException(404, "Unmatched report file not found")

    try:
        doc = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(500, "Could not read unmatched report")
    return JSONResponse(doc)


@app.get("/credits", response_class=HTMLResponse)
def credits_page(request: Request):
    ctx = Ctx(request)
    try:
        return ctx.render("credits.html", {"active_sheet": None})
    finally:
        ctx.close()


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", error: str = ""):
    ctx = Ctx(request)
    try:
        theme_state = ctx.theme_state
        catalog = theme_state["catalog"] if isinstance(theme_state.get("catalog"), dict) else {}
        themes = catalog.get("themes") if isinstance(catalog.get("themes"), list) else []
        invalid_themes = catalog.get("invalid") if isinstance(catalog.get("invalid"), list) else []
        value_cap_rows = db.classes_jobs_cap_rows(ctx.conn, ctx.run_id)
        return ctx.render("settings.html", {
            "active_sheet": None,
            "saved": saved,
            "error": error,
            "themes": themes,
            "invalid_themes": invalid_themes,
            "selected_theme": theme_state.get("theme") or {},
            "selected_theme_id": theme_state["theme_id"],
            "selected_scheme_setting": theme_state["scheme_setting"],
            "selected_section_sort_mode": ctx.section_sort_mode,
            "section_sort_options": SECTION_SORT_OPTIONS,
            "effective_scheme": theme_state["effective_scheme"],
            "value_cap_rows": value_cap_rows,
        })
    finally:
        ctx.close()


@app.post("/settings/theme")
def settings_theme_save(
    theme_id: str = Form(""),
    theme_scheme: str = Form("default"),
    section_sort_mode: str = Form(section_sort.DEFAULT_SORT_MODE),
):
    catalog = get_theme_catalog()
    themes_raw = catalog.get("themes")
    themes = [t for t in themes_raw if isinstance(t, dict)] if isinstance(themes_raw, list) else []
    by_id = {str(theme.get("id")): theme for theme in themes}

    normalized_theme_id = (theme_id or "").strip()
    selected_theme = by_id.get(normalized_theme_id)
    if selected_theme is None:
        return RedirectResponse(
            f"/settings?error={quote('Choose a valid theme.')}",
            status_code=303,
        )

    normalized_scheme = normalize_theme_scheme_setting(theme_scheme)
    normalized_section_sort_mode = section_sort.normalize_sort_mode(section_sort_mode)
    selected_schemes_raw = selected_theme.get("schemes")
    selected_schemes: dict[str, Any] = (
        selected_schemes_raw if isinstance(selected_schemes_raw, dict) else {}
    )
    if normalized_scheme in {"dark", "light"} and normalized_scheme not in selected_schemes:
        normalized_scheme = "default"

    response = RedirectResponse("/settings?saved=Settings%20updated", status_code=303)
    set_theme_cookie(response, normalized_theme_id)
    set_theme_scheme_cookie(response, normalized_scheme)
    set_section_sort_cookie(response, normalized_section_sort_mode)
    return response


@app.post("/settings/value-caps")
async def settings_value_caps_save(request: Request):
    form = await request.form()
    keys = [str(v).strip() for v in form.getlist("cap_key")]
    values = [str(v).strip() for v in form.getlist("cap_value")]
    defaults = [str(v).strip() for v in form.getlist("default_cap")]

    if not keys or len(keys) != len(values) or len(keys) != len(defaults):
        return RedirectResponse(
            f"/settings?error={quote('Could not parse cap settings form.')}",
            status_code=303,
        )

    overrides = db.load_value_cap_overrides()
    for key, raw_value, raw_default in zip(keys, values, defaults):
        if not key:
            continue
        try:
            cap = float(raw_value)
            default_cap = float(raw_default)
        except ValueError:
            return RedirectResponse(
                f"/settings?error={quote('Caps must be numeric values.')}",
                status_code=303,
            )
        if cap <= 0:
            return RedirectResponse(
                f"/settings?error={quote('Caps must be greater than 0.')}",
                status_code=303,
            )
        if abs(cap - default_cap) <= 1e-9:
            overrides.pop(key, None)
        else:
            overrides[key] = cap

    db.save_value_cap_overrides(overrides)
    conn = db.get_connection()
    try:
        db.clear_progress_rollups(conn)
    finally:
        conn.close()
    return RedirectResponse("/settings?saved=Max%20level%20caps%20updated", status_code=303)


@app.get("/lodestone-probe", response_class=HTMLResponse)
def lodestone_probe_page(
    request: Request,
    error: str = "",
    saved: str = "",
    run_id: str = "",
):
    ctx = Ctx(request)
    try:
        saved_url = cookie_lodestone_url(request)
        run = None
        if run_id:
            with LODESTONE_RUNS_LOCK:
                run = LODESTONE_RUNS.get(run_id)
        return ctx.render("lodestone_probe.html", {
            "lodestone_url": saved_url,
            "cookie_source": cookie_lodestone_browser(request),
            "include_standard": cookie_lodestone_include_standard(request),
            "error": error,
            "saved": saved,
            "run_id": run_id,
            "run": run,
            "active_sheet": None,
        })
    finally:
        ctx.close()


@app.post("/lodestone-probe/save")
def lodestone_probe_save(lodestone_url: str = Form("")):
    normalized = normalize_lodestone_url(lodestone_url)
    if normalized is None:
        return RedirectResponse(
            f"/lodestone-probe?error={quote('Use a valid Lodestone URL under finalfantasyxiv.com/lodestone/...')}",
            status_code=303,
        )
    response = RedirectResponse("/lodestone-probe", status_code=303)
    set_lodestone_cookie(response, normalized)
    return response


@app.post("/lodestone-probe/run")
def lodestone_probe_run(
    lodestone_url: str = Form(""),
    cookie_source: str = Form("edge"),
    include_standard: str = Form("0"),
    extra_paths: str = Form(""),
):
    normalized_url = normalize_lodestone_url(lodestone_url)
    if normalized_url is None:
        return RedirectResponse(
            f"/lodestone-probe?error={quote('Use a valid Lodestone URL under finalfantasyxiv.com/lodestone/...')}",
            status_code=303,
        )

    normalized_source = normalize_cookie_source(cookie_source)
    if normalized_source is None:
        return RedirectResponse(
            f"/lodestone-probe?error={quote('Choose Edge, Chrome, or Firefox as the cookie source.')}",
            status_code=303,
        )

    include_standard_pages = include_standard == "1"
    run_id = uuid.uuid4().hex
    log_path = LODESTONE_LOG_DIR / f"{run_id}.log"
    _append_lodestone_log_file(
        log_path,
        f"[{dt.datetime.now().strftime('%H:%M:%S')}] Job created for URL: {normalized_url}",
    )
    _prune_files_by_pattern(LODESTONE_LOG_DIR, pattern="*.log")
    with LODESTONE_RUNS_LOCK:
        LODESTONE_RUNS[run_id] = {
            "id": run_id,
            "status": "queued",
            "log_path": str(log_path),
            "logs": [f"[{dt.datetime.now().strftime('%H:%M:%S')}] Job queued"],
        }
    worker = threading.Thread(
        target=_run_lodestone_probe_job,
        args=(run_id, normalized_url, normalized_source, include_standard_pages, extra_paths),
        daemon=True,
    )
    worker.start()
    response = RedirectResponse(
        f"/lodestone-probe?run_id={quote(run_id)}",
        status_code=303,
    )
    set_lodestone_cookie(response, normalized_url)
    set_lodestone_browser_cookie(response, normalized_source)
    set_lodestone_include_standard_cookie(response, include_standard_pages)
    return response


@app.get("/lodestone-probe/status")
def lodestone_probe_status(run_id: str):
    with LODESTONE_RUNS_LOCK:
        run = LODESTONE_RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "Unknown run id")
        logs = run.get("logs") or []
        payload = {
            "id": run.get("id"),
            "status": run.get("status"),
            "error": run.get("error"),
            "saved_path": run.get("saved_path"),
            "log_path": run.get("log_path"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "log_tail": "\n".join(logs[-120:]),
        }
    return JSONResponse(payload)


# --- HTMX partials ----------------------------------------------------------

def _render_row(
    ctx: Ctx,
    sheet: dict,
    row_index: int,
    columns: list,
    chain_info: dict | None = None,
    *,
    oob: bool = False,
) -> str:
    row = db.fetch_row(
        ctx.conn, ctx.run_id, ctx.character_id, sheet["sheet_name"], row_index,
        ctx.starting_class,
    )
    if row is None:
        raise HTTPException(404, "Row not found")
    return templates.get_template("partials/row.html").render(
        request=ctx.request,
        sheet=sheet,
        columns=columns,
        row=row,
        chain=chain_info,
        oob=oob,
    )


@app.post("/api/toggle", response_class=HTMLResponse)
def api_toggle(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
):
    """Toggle a row's state. For chain rows transitioning todo→done, also
    auto-cascade the prerequisites; cascaded rows ride along as out-of-band
    `<tr>` fragments so HTMX patches them in place."""
    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)
        sheet = ctx.require_content_sheet(sheet_name)

        current_state = db.effective_state(
            ctx.conn,
            ctx.character_id,
            ctx.run_id,
            sheet_name,
            row_index,
            ctx.starting_class,
        )
        next_state = db.NEXT_STATE.get(current_state, "done")
        planned_rows: list[int]
        if db.is_chain_row(ctx.conn, ctx.run_id, sheet_name, row_index) and next_state == "done":
            planned_rows = _rows_to_complete_for_history(
                ctx.conn,
                character_id=ctx.character_id,
                run_id=ctx.run_id,
                sheet_name=sheet_name,
                row_index=row_index,
                starting_class=ctx.starting_class,
            )
        elif db.is_chain_row(ctx.conn, ctx.run_id, sheet_name, row_index) and current_state == "done":
            planned_rows = _rows_to_revert_for_history(
                ctx.conn,
                character_id=ctx.character_id,
                run_id=ctx.run_id,
                sheet_name=sheet_name,
                row_index=row_index,
                new_state=next_state,
                starting_class=ctx.starting_class,
            )
        else:
            planned_rows = [row_index]

        before_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=planned_rows,
            starting_class=ctx.starting_class,
        )

        _, changed = db.toggle_row(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        after_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[int(i) for i in changed],
            starting_class=ctx.starting_class,
        )
        history_step = _history_step(
            action="Toggle row",
            changes=_snapshot_diff_changes(before_snapshots, after_snapshots),
            run_id=ctx.run_id,
            character_id=ctx.character_id,
        )

        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        # the clicked row replaces #row-{row_index} via the main swap; the
        # rest piggyback as OOB swaps keyed by their own ids
        parts: list[str] = []
        for idx in sorted(set(changed) - {row_index}):
            parts.append(_render_row(ctx, sheet, idx, columns, flags.get(idx), oob=True))
        parts.append(_render_row(ctx, sheet, row_index, columns, flags.get(row_index)))
        resp = HTMLResponse("\n".join(parts))
        _set_hx_triggers(resp, history_step)
        return resp
    finally:
        ctx.close()


@app.post("/api/set-value", response_class=HTMLResponse)
def api_set_value(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
    percent: float = Form(...),
):
    """Set a value-row's numeric level (0..row cap); state is derived."""
    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)
        sheet = ctx.require_content_sheet(sheet_name)
        before_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[row_index],
            starting_class=ctx.starting_class,
        )
        db.set_row_value(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            percent, starting_class=ctx.starting_class,
        )
        after_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[row_index],
            starting_class=ctx.starting_class,
        )
        history_step = _history_step(
            action="Set value",
            changes=_snapshot_diff_changes(before_snapshots, after_snapshots),
            run_id=ctx.run_id,
            character_id=ctx.character_id,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        _set_hx_triggers(resp, history_step)
        return resp
    finally:
        ctx.close()


@app.post("/api/toggle-excluded", response_class=HTMLResponse)
def api_toggle_excluded(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
):
    """Two-state exclude toggle for value rows."""
    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)
        sheet = ctx.require_content_sheet(sheet_name)
        before_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[row_index],
            starting_class=ctx.starting_class,
        )
        db.toggle_excluded(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        after_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[row_index],
            starting_class=ctx.starting_class,
        )
        history_step = _history_step(
            action="Toggle excluded",
            changes=_snapshot_diff_changes(before_snapshots, after_snapshots),
            run_id=ctx.run_id,
            character_id=ctx.character_id,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        _set_hx_triggers(resp, history_step)
        return resp
    finally:
        ctx.close()


@app.post("/api/set-state", response_class=HTMLResponse)
def api_set_state(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
    state: str = Form(...),
):
    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)
        sheet = ctx.require_content_sheet(sheet_name)
        before_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[row_index],
            starting_class=ctx.starting_class,
        )
        db.set_row_state(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index, state,
            starting_class=ctx.starting_class,
        )
        after_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[row_index],
            starting_class=ctx.starting_class,
        )
        history_step = _history_step(
            action="Set state",
            changes=_snapshot_diff_changes(before_snapshots, after_snapshots),
            run_id=ctx.run_id,
            character_id=ctx.character_id,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        _set_hx_triggers(resp, history_step)
        return resp
    finally:
        ctx.close()


@app.post("/api/complete-chain", response_class=HTMLResponse)
def api_complete_chain(
    request: Request,
    sheet_name: str = Form(...),
    row_index: int = Form(...),
):
    """Mark a row and all its prerequisites done. Returns out-of-band row
    fragments so HTMX patches every changed `<tr>` in place — no page reload."""
    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)
        sheet = ctx.require_content_sheet(sheet_name)
        before_rows = _rows_to_complete_for_history(
            ctx.conn,
            character_id=ctx.character_id,
            run_id=ctx.run_id,
            sheet_name=sheet_name,
            row_index=row_index,
            starting_class=ctx.starting_class,
        )
        before_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=before_rows,
            starting_class=ctx.starting_class,
        )
        changed = db.complete_with_prerequisites(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        after_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=[int(i) for i in changed],
            starting_class=ctx.starting_class,
        )
        history_step = _history_step(
            action="Complete chain",
            changes=_snapshot_diff_changes(before_snapshots, after_snapshots),
            run_id=ctx.run_id,
            character_id=ctx.character_id,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        # one flags fetch covers every row we need to re-render
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        # also re-render visible neighbors that may have unblocked. Cheap: just
        # the row right after each changed one (same section).
        to_render = sorted(set(changed))
        parts = [
            _render_row(ctx, sheet, idx, columns, flags.get(idx), oob=True)
            for idx in to_render
        ]
        resp = HTMLResponse("\n".join(parts))
        _set_hx_triggers(resp, history_step)
        return resp
    finally:
        ctx.close()


@app.post("/api/bulk-set-section", response_class=HTMLResponse)
def api_bulk_set_section(
    request: Request,
    sheet_name: str = Form(...),
    target_state: str = Form(...),
    row_indices_json: str = Form("[]"),
    chain_done: str = Form("1"),
):
    if target_state not in {"done", "todo", "excluded"}:
        raise HTTPException(400, "Unsupported bulk target state")

    try:
        raw_indices = json.loads(row_indices_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Invalid row index payload: {exc}") from exc
    if not isinstance(raw_indices, list):
        raise HTTPException(400, "Row index payload must be a list")

    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)
        sheet = ctx.require_content_sheet(sheet_name)
        row_indices_set: set[int] = set()
        for raw in raw_indices:
            try:
                row_indices_set.add(int(raw))
            except (TypeError, ValueError):
                continue
        row_indices = sorted(row_indices_set)
        checkbox_rows: list[int] = []
        for idx in row_indices:
            snap = _row_snapshot(
                ctx.conn,
                run_id=ctx.run_id,
                character_id=ctx.character_id,
                sheet_name=sheet_name,
                row_index=idx,
                starting_class=ctx.starting_class,
            )
            if snap is None:
                continue
            if snap.get("row_type") == "checkbox":
                checkbox_rows.append(idx)

        apply_chain_done = target_state == "done" and chain_done == "1"
        planned_rows: list[int] = []
        if apply_chain_done:
            for idx in checkbox_rows:
                planned_rows.extend(
                    _rows_to_complete_for_history(
                        ctx.conn,
                        character_id=ctx.character_id,
                        run_id=ctx.run_id,
                        sheet_name=sheet_name,
                        row_index=idx,
                        starting_class=ctx.starting_class,
                    )
                )
        else:
            for idx in checkbox_rows:
                current_state = db.effective_state(
                    ctx.conn,
                    ctx.character_id,
                    ctx.run_id,
                    sheet_name,
                    idx,
                    ctx.starting_class,
                )
                if current_state != target_state:
                    planned_rows.append(idx)

        planned_rows = sorted(set(planned_rows))
        before_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=planned_rows,
            starting_class=ctx.starting_class,
        )

        changed: set[int] = set()
        if apply_chain_done:
            for idx in checkbox_rows:
                changed.update(
                    db.complete_with_prerequisites(
                        ctx.conn,
                        ctx.character_id,
                        ctx.run_id,
                        sheet_name,
                        idx,
                        ctx.starting_class,
                    )
                )
        else:
            with progress_io.batch(ctx.conn, ctx.character_id):
                for idx in checkbox_rows:
                    current_state = db.effective_state(
                        ctx.conn,
                        ctx.character_id,
                        ctx.run_id,
                        sheet_name,
                        idx,
                        ctx.starting_class,
                    )
                    if current_state == target_state:
                        continue
                    db.set_row_state(
                        ctx.conn,
                        ctx.character_id,
                        ctx.run_id,
                        sheet_name,
                        idx,
                        target_state,
                        commit=False,
                        starting_class=ctx.starting_class,
                    )
                    changed.add(idx)
            if changed:
                ctx.conn.commit()

        changed_rows = sorted(set(int(i) for i in changed))
        after_snapshots = _row_snapshots(
            ctx.conn,
            run_id=ctx.run_id,
            character_id=ctx.character_id,
            sheet_name=sheet_name,
            row_indices=changed_rows,
            starting_class=ctx.starting_class,
        )
        history_step = _history_step(
            action=f"Bulk set section to {target_state}",
            changes=_snapshot_diff_changes(before_snapshots, after_snapshots),
            run_id=ctx.run_id,
            character_id=ctx.character_id,
        )

        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn,
            ctx.run_id,
            ctx.character_id,
            sheet_name,
            ctx.starting_class,
        )
        parts = [
            _render_row(ctx, sheet, idx, columns, flags.get(idx), oob=True)
            for idx in changed_rows
        ]
        resp = HTMLResponse("\n".join(parts))
        _set_hx_triggers(resp, history_step)
        return resp
    finally:
        ctx.close()


@app.post("/api/history/apply", response_class=HTMLResponse)
def api_history_apply(
    request: Request,
    direction: str = Form(...),
    step_json: str = Form(...),
    current_sheet: str = Form(""),
):
    if direction not in {"undo", "redo"}:
        raise HTTPException(400, "Direction must be 'undo' or 'redo'")

    try:
        step = json.loads(step_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Invalid history payload: {exc}") from exc

    if not isinstance(step, dict):
        raise HTTPException(400, "History payload must be an object")
    raw_changes = step.get("changes")
    if not isinstance(raw_changes, list):
        raise HTTPException(400, "History payload missing change list")

    ctx = Ctx(request, full=False)
    try:
        _ensure_character_import_idle(ctx.character_id)

        step_character_id: int | None = None
        try:
            step_character_id_raw = step.get("character_id")
            if step_character_id_raw is not None:
                step_character_id = int(step_character_id_raw)
        except (TypeError, ValueError):
            step_character_id = None
        if step_character_id is not None and step_character_id != ctx.character_id:
            msg = (
                "This history step belongs to a different character. "
                "Switch to the original character or discard this stale step."
            )
            return HTMLResponse(
                "",
                status_code=409,
                headers={
                    "X-History-Error": msg,
                    "HX-Trigger": json.dumps({"history-stale": {"reason": "character-mismatch"}}),
                },
            )

        step_run_id: int | None = None
        try:
            step_run_id_raw = step.get("run_id")
            if step_run_id_raw is not None:
                step_run_id = int(step_run_id_raw)
        except (TypeError, ValueError):
            step_run_id = None
        if step_run_id is not None and step_run_id != ctx.run_id:
            msg = (
                "This history step was recorded against an older workbook run. "
                "Refresh and continue from current data."
            )
            return HTMLResponse(
                "",
                status_code=409,
                headers={
                    "X-History-Error": msg,
                    "HX-Trigger": json.dumps({"history-stale": {"reason": "run-mismatch"}}),
                },
            )

        parsed_changes: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for change in raw_changes:
            if not isinstance(change, dict):
                continue
            sheet_name = str(change.get("sheet_name") or "")
            if not sheet_name:
                continue
            try:
                row_index_raw = change.get("row_index")
                if row_index_raw is None:
                    continue
                row_index = int(row_index_raw)
            except (TypeError, ValueError):
                continue

            before = _normalize_history_snapshot(change.get("before"))
            after = _normalize_history_snapshot(change.get("after"))
            if before is None or after is None:
                continue

            expected = after if direction == "undo" else before
            target = before if direction == "undo" else after
            current = _row_snapshot(
                ctx.conn,
                run_id=ctx.run_id,
                character_id=ctx.character_id,
                sheet_name=sheet_name,
                row_index=row_index,
                starting_class=ctx.starting_class,
            )
            if not _history_snapshot_matches(expected, current):
                conflicts.append({
                    "sheet_name": sheet_name,
                    "row_index": row_index,
                })
                continue

            parsed_changes.append({
                "sheet_name": sheet_name,
                "row_index": row_index,
                "row_type": str(change.get("row_type") or "checkbox"),
                "target": target,
            })

        if conflicts:
            msg = (
                "Undo/redo step is stale because current row state no longer matches the "
                "expected preconditions. Refresh and continue from current state."
            )
            return HTMLResponse(
                "",
                status_code=409,
                headers={
                    "X-History-Error": msg,
                    "HX-Trigger": json.dumps({
                        "history-stale": {
                            "reason": "precondition-failed",
                            "count": len(conflicts),
                        }
                    }),
                },
            )

        if not parsed_changes and raw_changes:
            msg = "Undo/redo step contains no applicable row changes for the current workbook state."
            return HTMLResponse(
                "",
                status_code=409,
                headers={
                    "X-History-Error": msg,
                    "HX-Trigger": json.dumps({"history-stale": {"reason": "no-applicable-changes"}}),
                },
            )

        touched_by_sheet: dict[str, set[int]] = {}
        with progress_io.batch(ctx.conn, ctx.character_id):
            for change in parsed_changes:
                sheet_name = str(change.get("sheet_name") or "")
                row_index = int(change.get("row_index") or 0)
                row_type = str(change.get("row_type") or "checkbox")
                target_raw = change.get("target")
                target: dict[str, Any] = target_raw if isinstance(target_raw, dict) else {}
                target_state = str(target.get("state") or "todo")
                target_pct = target.get("progress_percent")
                target_explicit = bool(target.get("explicit")) if "explicit" in target else True

                try:
                    if not target_explicit:
                        db.clear_row_override(
                            ctx.conn,
                            ctx.character_id,
                            ctx.run_id,
                            sheet_name,
                            row_index,
                            commit=False,
                            starting_class=ctx.starting_class,
                        )
                    elif row_type == "value":
                        if isinstance(target_pct, (int, float)):
                            db.set_row_value(
                                ctx.conn,
                                ctx.character_id,
                                ctx.run_id,
                                sheet_name,
                                row_index,
                                float(target_pct),
                                commit=False,
                                starting_class=ctx.starting_class,
                            )
                            if target_state == "excluded":
                                db.set_row_state(
                                    ctx.conn,
                                    ctx.character_id,
                                    ctx.run_id,
                                    sheet_name,
                                    row_index,
                                    "excluded",
                                    commit=False,
                                    starting_class=ctx.starting_class,
                                )
                        else:
                            db.set_row_state(
                                ctx.conn,
                                ctx.character_id,
                                ctx.run_id,
                                sheet_name,
                                row_index,
                                target_state,
                                commit=False,
                                starting_class=ctx.starting_class,
                            )
                    else:
                        db.set_row_state(
                            ctx.conn,
                            ctx.character_id,
                            ctx.run_id,
                            sheet_name,
                            row_index,
                            target_state,
                            commit=False,
                            starting_class=ctx.starting_class,
                        )
                except ValueError as exc:
                    raise HTTPException(400, f"Invalid history change for row {sheet_name}:{row_index}: {exc}") from exc

                touched_by_sheet.setdefault(sheet_name, set()).add(row_index)
        if touched_by_sheet:
            ctx.conn.commit()

        body_parts: list[str] = []
        render_sheet = current_sheet.strip()
        if render_sheet and render_sheet in touched_by_sheet:
            import json as _json
            sheet = ctx.require_content_sheet(render_sheet)
            columns = _json.loads(sheet["data_columns_json"])
            flags = db.sheet_chain_flags(
                ctx.conn,
                ctx.run_id,
                ctx.character_id,
                render_sheet,
                ctx.starting_class,
            )
            for idx in sorted(touched_by_sheet.get(render_sheet, set())):
                body_parts.append(
                    _render_row(ctx, sheet, idx, columns, flags.get(idx), oob=True)
                )

        resp = HTMLResponse("\n".join(body_parts))
        resp.headers["HX-Trigger"] = json.dumps({"progress-changed": True})
        return resp
    finally:
        ctx.close()


@app.get("/api/chain/{sheet_name}/{row_index}", response_class=HTMLResponse)
def api_chain(request: Request, sheet_name: str, row_index: int):
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        row = db.fetch_row(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name, row_index,
            ctx.starting_class,
        )
        chain = db.fetch_chain(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name, row_index,
            ctx.starting_class,
        )
        return templates.TemplateResponse(
            ctx.request,
            "partials/chain_panel.html",
            {"request": request, "sheet": sheet, "row": row, "chain": chain},
        )
    finally:
        ctx.close()


@app.get("/api/progress-header", response_class=HTMLResponse)
def api_progress_header(request: Request, sheet_name: str = ""):
    """Re-rendered after a toggle so the overall + sheet bars stay live."""
    ctx = Ctx(request)
    try:
        header = ctx.header_context(sheet_name or None)
        return templates.TemplateResponse(
            ctx.request, "partials/progress_header.html",
            {"request": request, **header},
        )
    finally:
        ctx.close()


@app.get("/api/search", response_class=HTMLResponse)
def api_search(request: Request, q: str = ""):
    ctx = Ctx(request)
    try:
        results = db.search_nodes(
            ctx.conn, ctx.run_id, ctx.character_id, q,
            starting_class=ctx.starting_class,
        )
        return templates.TemplateResponse(
            ctx.request,
            "partials/search_results.html",
            {"request": request, "results": results, "q": q},
        )
    finally:
        ctx.close()


# --- character actions ------------------------------------------------------

@app.post("/characters/create")
def character_create(request: Request, name: str = Form(...)):
    conn = db.get_connection()
    try:
        try:
            cid = db.create_character(conn, name)
        except ValueError as exc:
            return RedirectResponse(f"/characters?error={quote(str(exc))}", status_code=303)
        except Exception:
            return RedirectResponse(
                f"/characters?error={quote('Name already exists')}", status_code=303
            )
    finally:
        conn.close()
    resp = RedirectResponse("/", status_code=303)
    set_char_cookie(resp, cid)
    return resp


@app.post("/characters/select")
def character_select(character_id: int = Form(...), next_url: str = Form("/")):
    target = next_url if next_url.startswith("/") else "/"
    resp = RedirectResponse(target, status_code=303)
    set_char_cookie(resp, character_id)
    return resp


@app.post("/characters/set-class")
def character_set_class(
    character_id: int = Form(...),
    starting_class: str = Form(""),
):
    cls = starting_class.strip().upper() or None
    conn = db.get_connection()
    try:
        try:
            db.set_character_class(conn, character_id, cls)
        except ValueError as exc:
            return RedirectResponse(
                f"/characters?error={quote(str(exc))}", status_code=303
            )
    finally:
        conn.close()
    return RedirectResponse("/characters", status_code=303)


@app.post("/characters/delete")
def character_delete(request: Request, character_id: int = Form(...)):
    conn = db.get_connection()
    try:
        try:
            fallback = db.delete_character(conn, character_id)
        except ValueError as exc:
            return RedirectResponse(
                f"/characters?error={quote(str(exc))}", status_code=303
            )
    finally:
        conn.close()
    active = cookie_character_id(request)
    resp = RedirectResponse("/characters", status_code=303)
    set_char_cookie(resp, fallback if active == character_id else (active or fallback))
    return resp


def _progress_report_destination(next_url: str) -> str:
    candidate = str(next_url or "").strip()
    return candidate if candidate.startswith("/") else "/progress-reports"


def _is_truthy_form_flag(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _find_report_review_item(
    report_doc: dict[str, Any],
    *,
    item_id: str,
) -> dict[str, Any] | None:
    review_items = report_doc.get("review_items")
    if not isinstance(review_items, list):
        return None
    for item in review_items:
        if isinstance(item, dict) and str(item.get("id") or "") == item_id:
            return item
    return None


def _apply_report_item_resolution_to_progress(
    conn,
    *,
    run_id: int,
    target_item: dict[str, Any],
    resolution: str,
    starting_class_cache: dict[int, str | None],
) -> str | None:
    normalized_resolution = str(resolution or "todo").strip().lower()
    if normalized_resolution == "todo":
        return None

    character_id = int(target_item.get("character_id") or 0)
    sheet_name = str(target_item.get("sheet_name") or "")
    row_index = int(target_item.get("row_index") or 0)
    row_type = str(target_item.get("row_type") or "checkbox")
    desired = target_item.get("after") if normalized_resolution == "done" else target_item.get("before")
    if not isinstance(desired, dict):
        raise ValueError("Report item snapshot data is missing.")

    desired_state = str(desired.get("state") or "todo").strip().lower()
    desired_value = desired.get("value")
    if desired_state not in {"done", "todo", "excluded"}:
        desired_state = "todo"

    node_exists = conn.execute(
        """
        SELECT 1 FROM nodes
        WHERE run_id = ? AND sheet_name = ? AND row_index = ?
        LIMIT 1
        """,
        (run_id, sheet_name, row_index),
    ).fetchone()
    if node_exists is None:
        raise ValueError("Target row no longer exists in the current workbook.")

    if character_id not in starting_class_cache:
        character = db.get_character(conn, character_id)
        if character is None:
            raise ValueError("Target character no longer exists.")
        starting_class_cache[character_id] = character["starting_class"]
    starting_class = starting_class_cache[character_id]

    if row_type == "value":
        if desired_state == "excluded":
            db.set_row_state(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
                "excluded",
                starting_class=starting_class,
            )
            return "excluded"
        if isinstance(desired_value, (int, float)):
            db.set_row_value(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
                float(desired_value),
                starting_class=starting_class,
            )
            return desired_state
        db.set_row_state(
            conn,
            character_id,
            run_id,
            sheet_name,
            row_index,
            desired_state,
            starting_class=starting_class,
        )
        return desired_state

    db.set_row_state(
        conn,
        character_id,
        run_id,
        sheet_name,
        row_index,
        desired_state,
        starting_class=starting_class,
    )
    return desired_state


@app.get("/progress-reports", response_class=HTMLResponse)
def progress_reports_page(
    request: Request,
    character_id: int | None = None,
    show_advanced: int = 0,
    error: str = "",
):
    ctx = Ctx(request)
    try:
        report_doc = _load_latest_progress_report()
        selected_character_id = int(character_id) if character_id is not None else ctx.character_id
        known_character_ids = {int(c["id"]) for c in ctx.characters}
        if selected_character_id not in known_character_ids:
            selected_character_id = ctx.character_id

        selected_character = next(
            (c for c in ctx.characters if int(c["id"]) == selected_character_id),
            ctx.character,
        )

        report_reason = ""
        report_generated_at = ""
        report_path = ""
        summary: dict[str, Any] = {}
        review_items: list[dict[str, Any]] = []
        advanced_items: list[dict[str, Any]] = []
        orphaned_map: dict[str, int] = {}

        if isinstance(report_doc, dict):
            report_reason = str(report_doc.get("reason") or "")
            report_generated_at = str(report_doc.get("generated_at") or "")
            report_path = str(report_doc.get("report_path") or "")
            summary_raw = report_doc.get("summary")
            summary = summary_raw if isinstance(summary_raw, dict) else {}

            review_items = progress_report.review_items_for_character(
                report_doc,
                selected_character_id,
            )
            review_items.sort(
                key=lambda item: (
                    str(item.get("sheet_name") or ""),
                    int(item.get("row_index") or 0),
                    str(item.get("label") or ""),
                )
            )

            advanced_raw = report_doc.get("advanced_items")
            if isinstance(advanced_raw, list):
                for item in advanced_raw:
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_character_id = int(item.get("character_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if item_character_id == selected_character_id:
                        advanced_items.append(item)

            advanced_meta = report_doc.get("advanced")
            if isinstance(advanced_meta, dict):
                orphans_raw = advanced_meta.get("orphaned_by_character")
                if isinstance(orphans_raw, dict):
                    orphaned_map = {
                        str(k): int(v)
                        for (k, v) in orphans_raw.items()
                        if isinstance(v, (int, float)) and int(v) > 0
                    }

        return ctx.render(
            "progress_reports.html",
            {
                "active_sheet": None,
                "report_doc": report_doc if isinstance(report_doc, dict) else None,
                "report_reason": report_reason,
                "report_generated_at": report_generated_at,
                "report_path": report_path,
                "report_summary": summary,
                "report_review_items": review_items,
                "report_advanced_items": advanced_items,
                "report_orphaned_map": orphaned_map,
                "report_show_advanced": bool(show_advanced),
                "report_selected_character": selected_character,
                "report_selected_character_id": selected_character_id,
                "error": error,
            },
        )
    finally:
        ctx.close()


@app.post("/progress-reports/resolve")
def progress_report_resolve_item(
    item_id: str = Form(...),
    resolution: str = Form("todo"),
    next_url: str = Form("/progress-reports"),
):
    destination = _progress_report_destination(next_url)
    normalized_resolution = str(resolution or "todo").strip().lower()
    if normalized_resolution not in progress_report.RESOLUTION_VALUES:
        return RedirectResponse(
            f"{destination}?error={quote('Invalid resolution state.')}",
            status_code=303,
        )

    report_doc = _load_latest_progress_report()
    if not isinstance(report_doc, dict):
        return RedirectResponse(
            f"{destination}?error={quote('No progress report found to resolve.')}",
            status_code=303,
        )

    target_item = _find_report_review_item(report_doc, item_id=item_id)
    if target_item is None:
        return RedirectResponse(
            f"{destination}?error={quote('Could not find that report item.')}",
            status_code=303,
        )

    applied_state: str | None = None
    if normalized_resolution != "todo":
        conn = db.get_connection()
        try:
            run_id = db.latest_run_id(conn)
            if run_id is None:
                raise ValueError("No ingest run found.")
            applied_state = _apply_report_item_resolution_to_progress(
                conn,
                run_id=run_id,
                target_item=target_item,
                resolution=normalized_resolution,
                starting_class_cache={},
            )

            _, current_run_token = _latest_run_identity(conn)
            _save_session_baseline_snapshot(
                conn,
                run_id,
                source="report_resolution",
                run_token=current_run_token,
            )
        except Exception as exc:
            conn.close()
            return RedirectResponse(
                f"{destination}?error={quote(str(exc))}",
                status_code=303,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    updated = progress_report.set_review_item_resolution(
        report_doc,
        item_id=item_id,
        status=normalized_resolution,
        applied_state=applied_state,
    )
    if updated is None:
        return RedirectResponse(
            f"{destination}?error={quote('Could not update report resolution state.')}",
            status_code=303,
        )

    report_path_raw = report_doc.get("report_path")
    report_path = Path(str(report_path_raw)) if isinstance(report_path_raw, str) and report_path_raw else None
    progress_report.save_report_document(report_doc, report_path=report_path)
    return RedirectResponse(destination, status_code=303)


@app.post("/progress-reports/resolve-bulk")
def progress_report_resolve_bulk(
    character_id: int = Form(...),
    resolution: str = Form("done"),
    only_unresolved: str = Form("1"),
    next_url: str = Form("/progress-reports"),
):
    destination = _progress_report_destination(next_url)
    normalized_resolution = str(resolution or "todo").strip().lower()
    if normalized_resolution not in progress_report.RESOLUTION_VALUES:
        return RedirectResponse(
            f"{destination}?error={quote('Invalid resolution state.')}",
            status_code=303,
        )

    report_doc = _load_latest_progress_report()
    if not isinstance(report_doc, dict):
        return RedirectResponse(
            f"{destination}?error={quote('No progress report found to resolve.')}",
            status_code=303,
        )

    target_items = progress_report.review_items_for_character(
        report_doc,
        character_id,
        include_resolved=True,
    )
    if _is_truthy_form_flag(only_unresolved):
        target_items = [
            item
            for item in target_items
            if str((item.get("resolution") or {}).get("status") or "todo").strip().lower() == "todo"
        ]

    if not target_items:
        return RedirectResponse(destination, status_code=303)

    applied_states: dict[str, str | None] = {}
    any_progress_updates = False

    if normalized_resolution != "todo":
        conn = db.get_connection()
        try:
            run_id = db.latest_run_id(conn)
            if run_id is None:
                raise ValueError("No ingest run found.")

            starting_class_cache: dict[int, str | None] = {}
            for item in target_items:
                item_id = str(item.get("id") or "")
                current_status = str((item.get("resolution") or {}).get("status") or "todo").strip().lower()
                if item_id and current_status == normalized_resolution:
                    continue
                applied_states[item_id] = _apply_report_item_resolution_to_progress(
                    conn,
                    run_id=run_id,
                    target_item=item,
                    resolution=normalized_resolution,
                    starting_class_cache=starting_class_cache,
                )
                any_progress_updates = True

            if any_progress_updates:
                _, current_run_token = _latest_run_identity(conn)
                _save_session_baseline_snapshot(
                    conn,
                    run_id,
                    source="report_resolution_bulk",
                    run_token=current_run_token,
                )
        except Exception as exc:
            conn.close()
            return RedirectResponse(
                f"{destination}?error={quote(str(exc))}",
                status_code=303,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    updated_count = 0
    for item in target_items:
        item_id = str(item.get("id") or "")
        updated = progress_report.set_review_item_resolution(
            report_doc,
            item_id=item_id,
            status=normalized_resolution,
            applied_state=applied_states.get(item_id),
        )
        if updated is not None:
            updated_count += 1

    if updated_count <= 0:
        return RedirectResponse(
            f"{destination}?error={quote('Could not update report resolution state.')}",
            status_code=303,
        )

    report_path_raw = report_doc.get("report_path")
    report_path = Path(str(report_path_raw)) if isinstance(report_path_raw, str) and report_path_raw else None
    progress_report.save_report_document(report_doc, report_path=report_path)
    return RedirectResponse(destination, status_code=303)


# --- export -----------------------------------------------------------------

@app.get("/export/current.csv")
def export_csv(request: Request):
    ctx = Ctx(request)
    try:
        rows = db.fetch_export_rows(
            ctx.conn, ctx.run_id, ctx.character_id, ctx.starting_class
        )
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["character", "sheet", "section", "row_index", "label", "state",
                    "progress_percent"])
        for r in rows:
            w.writerow([
                ctx.character["name"], r["sheet_name"], r["section_label"],
                r["row_index"], r["label"], r["state"], r["progress_percent"],
            ])
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        fname = f"ffxiv_{ctx.character['name'].replace(' ', '_')}.csv"
        return StreamingResponse(
            data, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    finally:
        ctx.close()


@app.get("/api/progress/between-run-report")
def api_between_run_report(
    request: Request,
    persist: bool = False,
    sample_limit: int = progress_report.SAMPLE_LIMIT_DEFAULT,
):
    ctx = Ctx(request, full=False)
    try:
        _, run_token = _latest_run_identity(ctx.conn)
        if run_token is None:
            raise HTTPException(503, "No ingest run found.")

        baseline = _load_session_baseline_snapshot()
        bounded_sample_limit = max(1, min(int(sample_limit), 200))
        report_doc, report_path = progress_report.create_between_run_report(
            ctx.conn,
            ctx.run_id,
            reason="api-request",
            run_token=run_token,
            sample_limit=bounded_sample_limit,
            persist=persist,
            baseline=baseline,
        )

        global LAST_BETWEEN_RUN_REPORT_PATH
        if report_path is not None:
            LAST_BETWEEN_RUN_REPORT_PATH = report_path

        payload = dict(report_doc)
        payload["report_path"] = str(report_path) if report_path is not None else None
        latest_path = progress_report.latest_report_path()
        payload["latest_report_path"] = (
            str(latest_path) if latest_path.exists() else None
        )
        return JSONResponse(payload)
    finally:
        ctx.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
