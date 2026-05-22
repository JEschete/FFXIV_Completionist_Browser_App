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

from app import db, desktop_import, lodestone_import, progress_io


def reconcile_progress_sidecars() -> None:
    """Make the DB match the per-character JSON sidecars (the source of truth
    for progress). One-time bootstraps any character that has DB progress but
    no sidecar yet; otherwise replays each sidecar's entries onto the current
    run's character_progress table. Detail logic lives in progress_io."""
    conn = db.get_connection()
    try:
        run_id = db.latest_run_id(conn)
        if run_id is None:
            print("Progress reconcile: skipped — no ingest run found "
                  "(run scripts/prep_xlsx_to_sqlite.py to populate the DB)")
            return
        report = progress_io.reconcile_all(conn, run_id)
        if report.characters:
            print("Progress reconcile:")
            print(report.summary())
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    reconcile_progress_sidecars()
    yield


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


def _run_character_import_job(
    run_id: str,
    *,
    character_id: int,
    payload_path: Path,
    clear_existing: bool,
) -> None:
    _update_character_import_run(
        run_id,
        status="running",
        started_at=dt.datetime.now().isoformat(),
    )
    _append_character_import_run_log(run_id, "Import job started")
    conn = db.get_connection()
    try:
        summary = lodestone_import.import_lodestone_payload(
            conn,
            character_id=character_id,
            payload_path=payload_path,
            clear_existing=clear_existing,
            progress=lambda msg: _append_character_import_run_log(run_id, msg),
        )
        unmatched_report_path = _write_unmatched_report(run_id, summary)
        _update_character_import_run(
            run_id,
            status="completed",
            finished_at=dt.datetime.now().isoformat(),
            unmatched_report_path=(str(unmatched_report_path) if unmatched_report_path else None),
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
                "unmatched_sample": summary.unmatched_items[:20],
            },
        )
        _append_character_import_run_log(run_id, "Import job completed")
    except Exception as exc:
        _update_character_import_run(
            run_id,
            status="failed",
            error=str(exc).strip() or exc.__class__.__name__,
            finished_at=dt.datetime.now().isoformat(),
        )
        _append_character_import_run_log(run_id, f"Import job failed: {exc}")
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


def _run_desktop_import_job(
    run_id: str,
    *,
    character_id: int,
    completion_path: Path,
    clear_existing: bool,
) -> None:
    _update_character_import_run(
        run_id,
        status="running",
        started_at=dt.datetime.now().isoformat(),
    )
    _append_character_import_run_log(run_id, "Desktop import job started")
    conn = db.get_connection()
    try:
        summary = desktop_import.import_desktop_completion(
            conn,
            character_id=character_id,
            completion_path=completion_path,
            clear_existing=clear_existing,
            progress=lambda msg: _append_character_import_run_log(run_id, msg),
        )
        unmatched_report_path = _write_unmatched_report(run_id, summary)
        _update_character_import_run(
            run_id,
            status="completed",
            finished_at=dt.datetime.now().isoformat(),
            unmatched_report_path=(str(unmatched_report_path) if unmatched_report_path else None),
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
                "unmatched_sample": summary.unmatched_items[:20],
            },
        )
        _append_character_import_run_log(run_id, "Desktop import job completed")
    except Exception as exc:
        _update_character_import_run(
            run_id,
            status="failed",
            error=str(exc).strip() or exc.__class__.__name__,
            finished_at=dt.datetime.now().isoformat(),
        )
        _append_character_import_run_log(run_id, f"Desktop import job failed: {exc}")
        _append_character_import_run_log(run_id, lodestone_import.format_exception(exc))
    finally:
        conn.close()


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
        run_id = db.latest_run_id(self.conn)
        if run_id is None:
            raise HTTPException(503, "No ingest run found. Run the prep script first.")
        self.run_id: int = run_id
        self.character = db.resolve_active_character(
            self.conn, cookie_character_id(request)
        )
        self.character_id = int(self.character["id"])
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

    def close(self) -> None:
        self.conn.close()

    def require_content_sheet(self, sheet_name: str) -> dict:
        """Lightweight single-sheet lookup for partial endpoints."""
        if self.full:
            sheet = self.sheets_by_name.get(sheet_name)
        else:
            row = db.fetch_sheet(self.conn, self.run_id, sheet_name)
            sheet = dict(row) if row is not None else None
        if sheet is None:
            raise HTTPException(404, f"Unknown sheet: {sheet_name}")
        if sheet["is_menu"]:
            raise HTTPException(400, "Not a content sheet")
        return sheet

    def base_context(self) -> dict:
        avatar_path = BASE / "static" / "avatars" / f"{self.character_id}.jpg"
        return {
            "request": self.request,
            "characters": self.characters,
            "character": self.character,
            "tree": self.tree,
            "overall": self.overall,
            "overall_pct": db.pct(self.overall),
            "avatar_url": f"/static/avatars/{self.character_id}.jpg" if avatar_path.exists() else None,
        }

    def header_context(self, sheet_name: str | None) -> dict:
        node = db.find_node(self.tree, sheet_name) if sheet_name else None
        roll = self.rollups.get(sheet_name) if sheet_name else None
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

        # content sheet: data-table view grouped by section
        rows = db.fetch_rows(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            q=q, state=state, starting_class=ctx.starting_class,
        )
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name, ctx.starting_class
        )
        for row in rows:
            row["chain_info"] = flags.get(row["row_index"])
        groups = db.group_rows_by_section(rows)
        roll = ctx.rollups.get(sheet_name, db._empty_roll())
        import json as _json
        return ctx.render("sheet.html", {
            "sheet": sheet,
            "columns": _json.loads(sheet["data_columns_json"]),
            "crumbs": crumbs,
            "groups": groups,
            "roll": roll,
            "pct": db.pct(roll),
            "q": q,
            "state": state,
            "shown": sum(len(g["rows"]) for g in groups),
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
):
    ctx = Ctx(request)
    try:
        run = None
        if run_id:
            with CHAR_IMPORT_RUNS_LOCK:
                run = CHAR_IMPORT_RUNS.get(run_id)

        suggested_payload = payload_path.strip()
        latest_payloads = sorted(
            LODESTONE_OUTPUT_DIR.glob("*_auth_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        payload_options = [str(p) for p in latest_payloads[:40]]
        if not suggested_payload and payload_options:
            suggested_payload = payload_options[0]

        desktop_options = [str(p) for p in desktop_import.list_detected_completion_files(limit=30)]
        suggested_desktop_path = desktop_path.strip()
        if not suggested_desktop_path and desktop_options:
            suggested_desktop_path = desktop_options[0]

        return ctx.render("characters.html", {
            "error": error,
            "starting_classes": db.STARTING_CLASSES,
            "import_run_id": run_id,
            "import_run": run,
            "suggested_payload": suggested_payload,
            "payload_options": payload_options,
            "suggested_desktop_path": suggested_desktop_path,
            "desktop_completion_options": desktop_options,
            "active_sheet": None,
        })
    finally:
        ctx.close()


@app.post("/characters/import-lodestone")
def character_import_lodestone(
    character_id: int = Form(...),
    payload_path: str = Form(""),
    payload_file: UploadFile | str | None = File(None),
    clear_existing: str = Form("0"),
):
    resolved_path: Path | None = None

    if isinstance(payload_file, UploadFile) and payload_file.filename:
        try:
            resolved_path = save_uploaded_payload(payload_file)
        except OSError as exc:
            return RedirectResponse(
                f"/characters?error={quote(f'Could not save uploaded payload: {exc}')}",
                status_code=303,
            )
    elif isinstance(payload_file, str) and payload_file.strip():
        # Backward compatibility for stale pages that still post filename text.
        resolved_path = resolve_payload_path(payload_file)
    else:
        resolved_path = resolve_payload_path(payload_path)

    if resolved_path is None:
        return RedirectResponse(
            f"/characters?error={quote('Choose a JSON payload file or pick a server payload below.')}",
            status_code=303,
        )
    if not resolved_path.exists() or not resolved_path.is_file():
        return RedirectResponse(
            f"/characters?error={quote(f'Payload file not found: {resolved_path}')}",
            status_code=303,
        )

    conn = db.get_connection()
    try:
        char = db.get_character(conn, character_id)
    finally:
        conn.close()
    if char is None:
        return RedirectResponse(
            f"/characters?error={quote(f'Character id {character_id} was not found')}",
            status_code=303,
        )

    run_id = uuid.uuid4().hex
    log_path = CHAR_IMPORT_LOG_DIR / f"{run_id}.log"
    clear_existing_flag = clear_existing == "1"
    _append_lodestone_log_file(
        log_path,
        f"[{dt.datetime.now().strftime('%H:%M:%S')}] Import created for character_id={character_id} payload={resolved_path}",
    )
    with CHAR_IMPORT_RUNS_LOCK:
        CHAR_IMPORT_RUNS[run_id] = {
            "id": run_id,
            "import_type": "lodestone-json",
            "status": "queued",
            "character_id": character_id,
            "character_name": char["name"],
            "payload_path": str(resolved_path),
            "clear_existing": clear_existing_flag,
            "log_path": str(log_path),
            "logs": [f"[{dt.datetime.now().strftime('%H:%M:%S')}] Import queued"],
        }

    worker = threading.Thread(
        target=_run_character_import_job,
        kwargs={
            "run_id": run_id,
            "character_id": character_id,
            "payload_path": resolved_path,
            "clear_existing": clear_existing_flag,
        },
        daemon=True,
    )
    worker.start()

    return RedirectResponse(
        f"/characters?run_id={quote(run_id)}&payload_path={quote(str(resolved_path))}",
        status_code=303,
    )


@app.post("/characters/import-desktop-app")
def character_import_desktop_app(
    character_id: int = Form(...),
    completion_path: str = Form(""),
    completion_file: UploadFile | str | None = File(None),
    clear_existing: str = Form("0"),
):
    resolved_path: Path | None = None

    if isinstance(completion_file, UploadFile) and completion_file.filename:
        try:
            resolved_path = save_uploaded_payload(completion_file)
        except OSError as exc:
            return RedirectResponse(
                f"/characters?error={quote(f'Could not save uploaded desktop completion file: {exc}')}",
                status_code=303,
            )
    elif isinstance(completion_file, str) and completion_file.strip():
        resolved_path = resolve_payload_path(completion_file)
    else:
        resolved_path = resolve_payload_path(completion_path)
        if resolved_path is None:
            detected = desktop_import.list_detected_completion_files(limit=1)
            if detected:
                resolved_path = detected[0]

    if resolved_path is None:
        return RedirectResponse(
            f"/characters?error={quote('Choose a desktop completion JSON file or pick a detected completion path.')}",
            status_code=303,
        )
    if not resolved_path.exists() or not resolved_path.is_file():
        return RedirectResponse(
            f"/characters?error={quote(f'Desktop completion file not found: {resolved_path}')}",
            status_code=303,
        )

    conn = db.get_connection()
    try:
        char = db.get_character(conn, character_id)
    finally:
        conn.close()
    if char is None:
        return RedirectResponse(
            f"/characters?error={quote(f'Character id {character_id} was not found')}",
            status_code=303,
        )

    run_id = uuid.uuid4().hex
    log_path = CHAR_IMPORT_LOG_DIR / f"{run_id}.log"
    clear_existing_flag = clear_existing == "1"
    _append_lodestone_log_file(
        log_path,
        f"[{dt.datetime.now().strftime('%H:%M:%S')}] Desktop import created for character_id={character_id} completion={resolved_path}",
    )
    with CHAR_IMPORT_RUNS_LOCK:
        CHAR_IMPORT_RUNS[run_id] = {
            "id": run_id,
            "import_type": "desktop-app",
            "status": "queued",
            "character_id": character_id,
            "character_name": char["name"],
            "payload_path": str(resolved_path),
            "clear_existing": clear_existing_flag,
            "log_path": str(log_path),
            "logs": [f"[{dt.datetime.now().strftime('%H:%M:%S')}] Desktop import queued"],
        }

    worker = threading.Thread(
        target=_run_desktop_import_job,
        kwargs={
            "run_id": run_id,
            "character_id": character_id,
            "completion_path": resolved_path,
            "clear_existing": clear_existing_flag,
        },
        daemon=True,
    )
    worker.start()

    return RedirectResponse(
        f"/characters?run_id={quote(run_id)}&desktop_path={quote(str(resolved_path))}",
        status_code=303,
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
            "unmatched_report_url": (
                f"/characters/import-unmatched?run_id={quote(str(run.get('id') or ''))}"
                if run.get("unmatched_report_path") else None
            ),
            "summary": summary,
            "log_tail": "\n".join(logs[-160:]),
        }
    return JSONResponse(payload)


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
        sheet = ctx.require_content_sheet(sheet_name)
        _, changed = db.toggle_row(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
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
        resp.headers["HX-Trigger"] = "progress-changed"
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
    """Set a value-row's numeric level (0-100); state is derived."""
    ctx = Ctx(request, full=False)
    try:
        sheet = ctx.require_content_sheet(sheet_name)
        db.set_row_value(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            percent, starting_class=ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        resp.headers["HX-Trigger"] = "progress-changed"
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
        sheet = ctx.require_content_sheet(sheet_name)
        db.toggle_excluded(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        resp.headers["HX-Trigger"] = "progress-changed"
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
        sheet = ctx.require_content_sheet(sheet_name)
        db.set_row_state(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index, state,
            starting_class=ctx.starting_class,
        )
        import json as _json
        columns = _json.loads(sheet["data_columns_json"])
        flags = db.sheet_chain_flags(
            ctx.conn, ctx.run_id, ctx.character_id, sheet_name,
            ctx.starting_class,
        )
        body = _render_row(ctx, sheet, row_index, columns, flags.get(row_index))
        resp = HTMLResponse(body)
        resp.headers["HX-Trigger"] = "progress-changed"
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
        sheet = ctx.require_content_sheet(sheet_name)
        changed = db.complete_with_prerequisites(
            ctx.conn, ctx.character_id, ctx.run_id, sheet_name, row_index,
            ctx.starting_class,
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
        resp.headers["HX-Trigger"] = "progress-changed"
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
