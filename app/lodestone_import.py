from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import sqlite3
import traceback
import unicodedata
import urllib.request
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from app import db, progress_io

_STATIC_AVATARS = Path(__file__).parent / "static" / "avatars"


def _save_avatar(payload: dict[str, Any], character_id: int, log: Callable[[str], None]) -> None:
    avatar_url = (payload.get("profile") or {}).get("images", {}).get("avatar")
    if not avatar_url:
        return
    dest = _STATIC_AVATARS / f"{character_id}.jpg"
    if dest.exists():
        return
    try:
        _STATIC_AVATARS.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(avatar_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest.write_bytes(resp.read())
        log(f"Avatar saved for character {character_id}")
    except Exception as exc:
        log(f"Avatar download skipped: {exc}")


_COMMON_CONFUSABLES = str.maketrans({
    "§": "s",
    "Α": "A",
    "α": "a",
    "Ι": "I",
    "ι": "i",
    "Æ": "AE",
    "æ": "ae",
    "Œ": "OE",
    "œ": "oe",
    "’": "'",
})


@dataclass
class ImportSummary:
    character_id: int
    character_name: str
    source_path: str
    run_id: int
    total_candidates: int
    matched_candidates: int
    unmatched_candidates: int
    rows_applied: int
    rows_skipped_already_done: int
    unmatched_items: list[dict[str, Any]]


LODESTONE_LEVEL_MERGE_MODES = {"keep-highest", "overwrite"}


def load_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Lodestone payload must be a JSON object")
    return data


def _norm_label(value: str) -> str:
    value = value.translate(_COMMON_CONFUSABLES)
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    # Treat apostrophes as optional (Vana'diel vs Vanadiel, etc.).
    folded = folded.replace("'", "")
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return folded.strip()


def _strip_marker_prefixes(value: str) -> str:
    # Lodestone sometimes prefixes quest names with icon glyphs or bullets.
    return re.sub(r"^[^A-Za-z0-9'\"]+", "", value).strip()


def _quest_label_aliases(raw: str) -> set[str]:
    """Generate likely quest label forms from Lodestone payload labels.

    Example: "Abalathian Sidequests (A Cropper's Duty)" ->
      - full string
      - inner: "A Cropper's Duty"
    """
    value = _strip_marker_prefixes(raw.strip())
    aliases = {value} if value else set()

    # Pull inner quest title from category wrappers.
    m = re.match(r"^(.+?)\s*\((.+)\)\s*$", value)
    if m:
        inner = _strip_marker_prefixes(m.group(2).strip())
        if inner:
            aliases.add(inner)

    # For labels like "Role Quests (Shadowbringers) (Quest Name)", use the
    # trailing parenthetical content as the likely actual quest title.
    m_last = re.search(r"\(([^()]*)\)\s*$", value)
    if m_last:
        inner_last = _strip_marker_prefixes(m_last.group(1).strip())
        if inner_last:
            aliases.add(inner_last)

    # Remove any leading marker glyphs if still present after normalization.
    cleaned = _strip_marker_prefixes(value)
    if cleaned:
        aliases.add(cleaned)

    return {a for a in aliases if a}


def _add_candidate(pool: dict[str, set[str]], bucket: str, raw: Any) -> None:
    if not isinstance(raw, str):
        return
    value = raw.strip()
    if not value:
        return

    # Lodestone may emit unknown Triple Triad placeholders; do not treat as
    # actionable unmatched items.
    if bucket in {"tripletriad", "bluemagic"} and re.fullmatch(r"[?？]+", value):
        return

    pool.setdefault(bucket, set()).add(value)


def collect_candidates(payload: dict[str, Any]) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = {
        "quest": set(),
        "achievement": set(),
        "minion": set(),
        "mount": set(),
        "tripletriad": set(),
        "bluemagic": set(),
        "emote": set(),
        "orchestrion": set(),
    }

    minions = payload.get("minions")
    if isinstance(minions, dict):
        items = minions.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _add_candidate(candidates, "minion", item.get("name"))

    mounts = payload.get("mounts")
    if isinstance(mounts, dict):
        items = mounts.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _add_candidate(candidates, "mount", item.get("name"))

    achievements = payload.get("achievements")
    if isinstance(achievements, dict):
        entries = achievements.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    _add_candidate(candidates, "achievement", entry.get("title"))

    auth_pages = payload.get("authenticated_pages")
    if isinstance(auth_pages, dict):
        quest_page = auth_pages.get("quest")
        if isinstance(quest_page, dict):
            entries = quest_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "quest", entry.get("title"))

        triad_page = auth_pages.get("goldsaucer/tripletriad")
        if isinstance(triad_page, dict):
            entries = triad_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "tripletriad", entry.get("name"))

        bluemage_page = auth_pages.get("bluemage")
        if isinstance(bluemage_page, dict):
            entries = bluemage_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "bluemagic", entry.get("name") or entry.get("title"))

        emote_page = auth_pages.get("emote")
        if isinstance(emote_page, dict):
            entries = emote_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "emote", entry.get("name") or entry.get("label"))

        orchestrion_page = auth_pages.get("orchestrion")
        if isinstance(orchestrion_page, dict):
            entries = orchestrion_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "orchestrion", entry.get("name") or entry.get("label"))

    return candidates


# Story/raid/relic sheets that represent quest completion but often do not
# contain the literal word "quest" in the sheet title.
_QUEST_LIKE_SHEET_TOKENS = (
    "quest",
    "primals",
    "bahamut",
    "the crystal tower",
    "alexander",
    "the warring triad",
    "the shadow of mach",
    "omega",
    "return to ivalice",
    "the four lords",
    "eden",
    "yorha dark apocalypse",
    "the sorrow of werlyt",
    "pandæmonium",
    "myths of the realm",
    "the arcadion",
    "echoes of vanadiel",
    "echoes of vana'diel",
    "chronicles of light",
    "hildibrand",
    "weapon enhancement",
    "records of unusual endeavors",
    "side story quests",
    "disciple of war quests",
    "disciple of magic quests",
    "disciple of the hand quests",
    "disciple of the land quests",
    "disciple of war job quests",
    "disciple of magic job quests",
    "role quests",
    "hall of the novice",
    "crystalline mean quests",
    "studium quests",
    "wachumeqimeqi quests",
    "relic tools",
    "relic weapons",
)

_QUEST_LIKE_SHEET_TOKENS_NORM = tuple(
    sorted({norm for token in _QUEST_LIKE_SHEET_TOKENS if (norm := _norm_label(token))})
)


def _sheet_buckets(sheet_name: str) -> set[str]:
    name_norm = _norm_label(sheet_name)
    out: set[str] = set()

    if any(token in name_norm for token in _QUEST_LIKE_SHEET_TOKENS_NORM):
        out.add("quest")
    if "achievement" in name_norm or "achiev" in name_norm:
        out.add("achievement")
    if "minion" in name_norm:
        out.add("minion")
    if "mount" in name_norm:
        out.add("mount")
    if "triple triad" in name_norm or "card" in name_norm:
        out.add("tripletriad")
    if "blue magic spellbook" in name_norm:
        out.add("bluemagic")
    if "emote" in name_norm:
        out.add("emote")
    if "orchestrion" in name_norm:
        out.add("orchestrion")
    return out


def _orchestrion_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}
    if value.casefold().endswith(" orchestrion roll"):
        aliases.add(value[:-17].strip())
    else:
        aliases.add(f"{value} Orchestrion Roll")
    return {a for a in aliases if a}


def _tripletriad_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}
    if value.casefold().endswith(" card"):
        base = value[:-5].strip()
        if base:
            aliases.add(base)
    else:
        aliases.add(f"{value} Card")

    # Some labels may include/omit a leading article.
    if value.casefold().startswith("the "):
        aliases.add(value[4:].strip())
    else:
        aliases.add(f"The {value}")

    # Lodestone and workbook may use '&' vs 'and' inconsistently.
    and_variants = set()
    for alias in aliases:
        and_variants.add(alias.replace("&", "and"))
        and_variants.add(re.sub(r"\band\b", "&", alias, flags=re.IGNORECASE))
    aliases.update(a.strip() for a in and_variants if a and a.strip())

    card_variants = set()
    for alias in aliases:
        if alias.casefold().endswith(" card"):
            card_variants.add(alias)
        else:
            card_variants.add(f"{alias} Card")
    aliases.update(card_variants)

    return aliases


_ROMAN_MAP = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}


def _suffix_roman_digit_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}

    m_roman = re.match(r"^(.*?)(?:\s+)(I|II|III|IV|V|VI|VII|VIII|IX|X)$", value)
    if m_roman:
        prefix = m_roman.group(1).strip()
        roman = m_roman.group(2)
        aliases.add(f"{prefix} {_ROMAN_MAP[roman]}")

    m_digit = re.match(r"^(.*?)(?:\s+)(\d{1,2})$", value)
    if m_digit:
        prefix = m_digit.group(1).strip()
        num = int(m_digit.group(2))
        for roman, mapped in _ROMAN_MAP.items():
            if mapped == num:
                aliases.add(f"{prefix} {roman}")
                break

    return aliases


def _achievement_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}

    # Lodestone and workbook may disagree on suffix style (I/II vs 1/2).
    m_roman = re.match(r"^(.*?)(?:\s+)(I|II|III|IV|V|VI|VII|VIII|IX|X)$", value)
    if m_roman:
        prefix = m_roman.group(1).strip()
        roman = m_roman.group(2)
        aliases.add(f"{prefix} {_ROMAN_MAP[roman]}")

    m_digit = re.match(r"^(.*?)(?:\s+)(\d{1,2})$", value)
    if m_digit:
        prefix = m_digit.group(1).strip()
        num = int(m_digit.group(2))
        for roman, mapped in _ROMAN_MAP.items():
            if mapped == num:
                aliases.add(f"{prefix} {roman}")
                break

    # Known Lodestone/workbook title variants.
    if "aetherfont" in value.casefold():
        aliases.add(re.sub(r"aetherfont", "Aetherfront", value, flags=re.IGNORECASE))

    if "good-bye" in value.casefold():
        aliases.add(re.sub(r"good-bye", "Goodbye", value, flags=re.IGNORECASE))

    if value.endswith("..."):
        aliases.add(value[:-3] + "…")

    return aliases


def _candidate_aliases(bucket: str, raw_label: str) -> list[str]:
    aliases: set[str]
    if bucket == "quest":
        aliases = _quest_label_aliases(raw_label)
    elif bucket == "tripletriad":
        aliases = _tripletriad_label_aliases(raw_label)
    elif bucket == "achievement":
        aliases = _achievement_label_aliases(raw_label)
    elif bucket == "orchestrion":
        aliases = _orchestrion_label_aliases(raw_label)
    else:
        aliases = {raw_label}

    expanded: set[str] = set()
    for alias in aliases:
        expanded.update(_suffix_roman_digit_aliases(alias))
    return sorted(a for a in expanded if a)


def _partial_match_hits(
    *,
    bucket: str,
    aliases: list[str],
    bucket_norm: dict[str, list[tuple[str, int, str]]],
    bucket_norm_keys: list[str],
) -> list[tuple[str, int, str]] | None:
    if not bucket_norm_keys:
        return None

    # Conservative by default; only rescue near-typo misses.
    cutoff = 0.9 if bucket == "quest" else 0.93
    if bucket == "tripletriad":
        cutoff = 0.85

    for alias in aliases:
        norm = _norm_label(alias)
        if len(norm) < 8:
            continue
        close = difflib.get_close_matches(norm, bucket_norm_keys, n=1, cutoff=cutoff)
        if close:
            hits = bucket_norm.get(close[0])
            if hits:
                return hits
    return None


def _index_labels_for_bucket(
    *,
    bucket: str,
    node_label: str,
    row_json_obj: dict[str, Any] | None,
) -> set[str]:
    labels = {node_label}
    if isinstance(row_json_obj, dict):
        field_by_bucket = {
            "quest": "quest",
            "bluemagic": "spell",
            "emote": "emote",
            "orchestrion": "orchestrion_roll",
        }
        field = field_by_bucket.get(bucket)
        if isinstance(field, str):
            value = row_json_obj.get(field)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    labels.add(text)
                    # Workbook sometimes combines two quest names with " / "
                    # (e.g. "Training with Leih / School of Hard Nocks").
                    # Index each part so either name from Lodestone matches.
                    if " / " in text:
                        for part in text.split(" / "):
                            part = part.strip()
                            if part:
                                labels.add(part)
    return labels


def _unmatched_reason(
    *,
    bucket: str,
    raw_label: str,
    aliases: list[str],
    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]],
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]],
) -> tuple[str, dict[str, Any]]:
    alias_norms = [_norm_label(a) for a in aliases if _norm_label(a)]

    other_buckets: set[str] = set()
    for alias in aliases:
        key = alias.casefold()
        for b, b_exact in exact_idx.items():
            if b == bucket:
                continue
            if key in b_exact:
                other_buckets.add(b)
    for alias_norm in alias_norms:
        for b, b_norm in norm_idx.items():
            if b == bucket:
                continue
            if alias_norm in b_norm:
                other_buckets.add(b)

    if other_buckets:
        return "mapped_to_other_bucket", {
            "other_buckets": sorted(other_buckets),
        }

    if bucket == "tripletriad":
        triad_norm = norm_idx.get("tripletriad", {})
        for alias in aliases:
            stripped = alias.strip()
            if not stripped:
                continue
            if stripped.casefold().endswith(" card"):
                alt = stripped[:-5].strip()
            else:
                alt = f"{stripped} Card"
            alt_norm = _norm_label(alt)
            if alt_norm and alt_norm in triad_norm:
                return "label_variant_card_suffix", {"suggested_alias": alt}

    if bucket == "quest" and re.match(r"^.+?\s*\(.+\)\s*$", raw_label.strip()):
        return "quest_wrapper_unresolved", {}

    return "not_found_in_workbook", {}


def _empty_sidecar(character: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": progress_io.SCHEMA_VERSION,
        "character": {
            "name": character["name"],
            "starting_class": character["starting_class"],
            "created_at": character["created_at"] or dt.datetime.now().isoformat(timespec="seconds"),
        },
        "progress": [],
    }


def reset_character_progress(
    conn: sqlite3.Connection,
    character: sqlite3.Row,
    run_id: int,
) -> None:
    conn.execute(
        "DELETE FROM character_progress WHERE character_id = ? AND run_id = ?",
        (character["id"], run_id),
    )
    conn.execute(
        "DELETE FROM progress_rollup WHERE character_id = ? AND run_id = ?",
        (character["id"], run_id),
    )
    conn.commit()

    sidecar = progress_io.sidecar_path(character["name"])
    progress_io.save_sidecar(sidecar, _empty_sidecar(character))
    progress_io.invalidate_cache(sidecar)


def _collect_lodestone_class_job_levels(payload: dict[str, Any]) -> dict[str, float]:
    """Return normalized Classes-Jobs label -> level values from Lodestone payload."""
    out: dict[str, float] = {}
    class_job = payload.get("class_job")
    if not isinstance(class_job, dict):
        return out

    for rows in class_job.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            job_name = str(row.get("job") or "").strip()
            if not job_name:
                continue
            level_raw = row.get("level")
            if not isinstance(level_raw, (int, float)):
                continue
            level = max(0.0, float(level_raw))
            key = _norm_label(job_name)
            if not key:
                continue
            prev = out.get(key)
            if prev is None or level > prev:
                out[key] = level
    return out


def _numeric_or_none(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.replace(",", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _apply_lodestone_class_job_levels(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    run_id: int,
    starting_class: str | None,
    levels_by_label: dict[str, float],
    merge_mode: str,
) -> tuple[int, int]:
    """Apply Lodestone class/job levels onto Classes-Jobs value rows.

    Returns (rows_applied, rows_skipped).
    """
    if not levels_by_label:
        return 0, 0

    rows = conn.execute(
        """
        SELECT n.row_index, n.label, n.row_json,
               s.value_key,
               p.progress_percent
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        WHERE n.run_id = ?
          AND n.sheet_name = 'Classes-Jobs'
          AND n.row_type = 'value'
          AND n.label IS NOT NULL
        ORDER BY n.row_index
        """,
        (character_id, run_id),
    ).fetchall()

    applied = 0
    skipped = 0

    for row in rows:
        label = str(row["label"] or "").strip()
        if not label:
            continue
        key = _norm_label(label)
        if not key:
            continue
        incoming = levels_by_label.get(key)
        if incoming is None:
            continue

        existing = _numeric_or_none(row["progress_percent"])
        if existing is None:
            row_json_obj: dict[str, Any] | None = None
            row_json_raw = row["row_json"]
            if isinstance(row_json_raw, str) and row_json_raw.strip():
                try:
                    decoded = json.loads(row_json_raw)
                    if isinstance(decoded, dict):
                        row_json_obj = decoded
                except json.JSONDecodeError:
                    row_json_obj = None
            value_key = str(row["value_key"] or "")
            if row_json_obj is not None and value_key:
                existing = _numeric_or_none(row_json_obj.get(value_key))
        if existing is None:
            existing = 0.0

        target = incoming if merge_mode == "overwrite" else max(existing, incoming)
        if abs(target - existing) < 1e-9:
            skipped += 1
            continue

        db.set_row_value(
            conn,
            character_id,
            run_id,
            "Classes-Jobs",
            int(row["row_index"]),
            target,
            commit=False,
            starting_class=starting_class,
        )
        applied += 1

    return applied, skipped


def import_lodestone_payload(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    payload_path: Path,
    clear_existing: bool = False,
    level_merge_mode: str = "keep-highest",
    progress: Callable[[str], None] | None = None,
) -> ImportSummary:
    def log(message: str) -> None:
        if progress is not None:
            progress(message)

    run_id = db.latest_run_id(conn)
    if run_id is None:
        raise ValueError("No ingest run found. Run scripts/prep_xlsx_to_sqlite.py first.")

    character = db.get_character(conn, character_id)
    if character is None:
        raise ValueError(f"Character id {character_id} was not found")

    log(f"Loading payload: {payload_path}")
    payload = load_payload(payload_path)
    _save_avatar(payload, character_id, log)
    level_mode = level_merge_mode if level_merge_mode in LODESTONE_LEVEL_MERGE_MODES else "keep-highest"
    class_job_levels = _collect_lodestone_class_job_levels(payload)
    if class_job_levels:
        log(
            "Collected Lodestone class/job levels: "
            f"{len(class_job_levels)} rows (merge mode: {level_mode})"
        )
    else:
        log("No class/job levels found in Lodestone payload")
    candidates = collect_candidates(payload)
    total_candidates = sum(len(v) for v in candidates.values())
    log(
        "Collected candidates: "
        + ", ".join(f"{bucket}={len(values)}" for bucket, values in candidates.items())
    )

    if clear_existing:
        log("Clearing existing character progress before import")
        reset_character_progress(conn, character, run_id)

    rows = conn.execute(
        """
                SELECT n.sheet_name, n.row_index, n.label, n.row_type, n.row_json
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        WHERE n.run_id = ?
          AND s.is_menu = 0
          AND n.label IS NOT NULL
          AND n.row_type IN ('checkbox', 'value')
        """,
        (run_id,),
    ).fetchall()

    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}

    for row in rows:
        sheet_name = row["sheet_name"]
        label = (row["label"] or "").strip()
        if not label:
            continue
        entry = (sheet_name, int(row["row_index"]), row["row_type"])
        row_json_obj: dict[str, Any] | None = None
        row_json_text = row["row_json"]
        if isinstance(row_json_text, str) and row_json_text.strip():
            try:
                decoded = json.loads(row_json_text)
                if isinstance(decoded, dict):
                    row_json_obj = decoded
            except json.JSONDecodeError:
                row_json_obj = None
        for bucket in _sheet_buckets(sheet_name):
            for idx_label in _index_labels_for_bucket(
                bucket=bucket,
                node_label=label,
                row_json_obj=row_json_obj,
            ):
                exact_idx.setdefault(bucket, {}).setdefault(idx_label.casefold(), []).append(entry)
                norm = _norm_label(idx_label)
                if norm:
                    norm_idx.setdefault(bucket, {}).setdefault(norm, []).append(entry)

    targets: dict[tuple[str, int], str] = {}
    norm_keys_idx: dict[str, list[str]] = {
        bucket: list(bucket_norm.keys()) for bucket, bucket_norm in norm_idx.items()
    }
    matched_candidates = 0
    unmatched_candidates = 0
    ignored_untracked_emotes = 0
    unmatched_items: list[dict[str, Any]] = []

    for bucket, labels in candidates.items():
        bucket_exact = exact_idx.get(bucket, {})
        bucket_norm = norm_idx.get(bucket, {})
        bucket_norm_keys = norm_keys_idx.get(bucket, [])
        for raw_label in sorted(labels):
            hits: list[tuple[str, int, str]] | None = None
            aliases = _candidate_aliases(bucket, raw_label)
            for alias in aliases:
                hits = bucket_exact.get(alias.casefold())
                if hits:
                    break
                hits = bucket_norm.get(_norm_label(alias))
                if hits:
                    break
            if not hits:
                hits = _partial_match_hits(
                    bucket=bucket,
                    aliases=aliases,
                    bucket_norm=bucket_norm,
                    bucket_norm_keys=bucket_norm_keys,
                )
            if not hits:
                reason, reason_extra = _unmatched_reason(
                    bucket=bucket,
                    raw_label=raw_label,
                    aliases=aliases,
                    exact_idx=exact_idx,
                    norm_idx=norm_idx,
                )

                # Lodestone includes many built-in/default emotes that are not
                # represented in the workbook. Keep reports focused on truly
                # actionable misses by skipping those untracked emote entries.
                if bucket == "emote" and reason in {"not_found_in_workbook", "mapped_to_other_bucket"}:
                    ignored_untracked_emotes += 1
                    continue

                unmatched_candidates += 1
                unmatched_item = {
                    "bucket": bucket,
                    "label": raw_label,
                    "reason": reason,
                    "attempted_aliases": aliases[:6],
                }
                unmatched_item.update(reason_extra)
                unmatched_items.append(unmatched_item)
                continue
            matched_candidates += 1
            for sheet_name, row_index, row_type in hits:
                targets[(sheet_name, row_index)] = row_type

    log(
        f"Matched {matched_candidates}/{total_candidates} candidates; "
        f"resolved to {len(targets)} workbook rows"
    )

    rows_applied = 0
    rows_skipped = 0
    level_rows_applied = 0
    level_rows_skipped = 0
    starting_class = character["starting_class"]

    with progress_io.batch(conn, character_id):
        ordered_targets = sorted(targets.items(), key=lambda item: (item[0][0], item[0][1]))
        for idx, ((sheet_name, row_index), row_type) in enumerate(ordered_targets, start=1):
            current = db.effective_state(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
                starting_class,
            )
            if current == "done":
                rows_skipped += 1
                continue

            if row_type == "value":
                db.set_row_value(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    100.0,
                    commit=False,
                    starting_class=starting_class,
                )
            else:
                db.set_row_state(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    "done",
                    commit=False,
                    starting_class=starting_class,
                )
            rows_applied += 1

            if idx % 50 == 0:
                log(f"Applied {idx}/{len(ordered_targets)} matched rows")

        level_rows_applied, level_rows_skipped = _apply_lodestone_class_job_levels(
            conn,
            character_id=character_id,
            run_id=run_id,
            starting_class=starting_class,
            levels_by_label=class_job_levels,
            merge_mode=level_mode,
        )

    rows_applied += level_rows_applied
    rows_skipped += level_rows_skipped

    conn.commit()

    log(
        f"Import complete: applied={rows_applied}, already_done={rows_skipped}, "
        f"unmatched_candidates={unmatched_candidates}"
    )
    if ignored_untracked_emotes:
        log(f"Ignored untracked emote candidates: {ignored_untracked_emotes}")
    if unmatched_items:
        reason_counts = Counter(str(item.get("reason") or "unknown") for item in unmatched_items)
        reason_msg = ", ".join(
            f"{reason}={count}" for reason, count in sorted(reason_counts.items())
        )
        log(f"Unmatched reasons: {reason_msg}")
        sample = ", ".join(
            f"{item['bucket']}:{item['label']} ({item.get('reason')})"
            for item in unmatched_items[:6]
        )
        log(f"Unmatched sample: {sample}")
    if class_job_levels:
        log(
            "Classes-Jobs level sync: "
            f"applied={level_rows_applied}, skipped={level_rows_skipped}"
        )

    return ImportSummary(
        character_id=character_id,
        character_name=character["name"],
        source_path=str(payload_path),
        run_id=run_id,
        total_candidates=total_candidates,
        matched_candidates=matched_candidates,
        unmatched_candidates=unmatched_candidates,
        rows_applied=rows_applied,
        rows_skipped_already_done=rows_skipped,
        unmatched_items=unmatched_items,
    )


def format_exception(exc: Exception) -> str:
    return "\n".join(traceback.format_exception(exc))

# --- Desktop completion import ---------------------------------------------

# Canonical buckets with established alias/matching behavior. Any other
# completion path group still imports via dynamic fallback buckets.
CANONICAL_BUCKETS = (
    "quest",
    "achievement",
    "minion",
    "mount",
    "tripletriad",
    "bluemagic",
    "emote",
    "orchestrion",
)

_SEGMENT_ALIASES = {
    "hunting-log": "hunting",
    "classes--jobs": "classes-jobs",
    "order-of-the-twin-adder": "twin-adder",
    "resplendent": "resplendent-tools",
    "dungeons": "dungeon",
    "trials": "trial",
    "raids": "raid",
}

_CANONICAL_SOURCE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "minion": ("character/adventure-plate/minion",),
    "mount": ("character/adventure-plate/mount",),
}

_PLACE_ZONE_NORM_ALIASES = {
    "raktika": "theraktikagreatwood",
}

_IGNORED_UNTRACKED_BUCKETS = {
    "character/gold-saucer/chocobo",
    "duty/squadron",
    "duty/squadron/stats",
}

_POSITIONAL_VALUE_BUCKETS = {
    "classes-jobs",
    "desynthesis",
}

_REFERENCE_ID_RE = re.compile(r"^([A-Za-z])\.(\d+)$")

_REFERENCE_BUCKET_BY_PREFIX = {
    "q": "quest",
    "a": "achievement",
}


def _path_group_key(parts: list[str]) -> str | None:
    segments: list[str] = []
    for raw_part in parts:
        cleaned = str(raw_part).strip().casefold()
        if not cleaned:
            continue
        cleaned = re.sub(r"-{2,}", "-", cleaned)
        cleaned = _SEGMENT_ALIASES.get(cleaned, cleaned)
        segments.append(cleaned)
    if not segments:
        return None

    if segments[0] == "overall":
        segments = segments[1:]
    if not segments:
        return None

    if re.fullmatch(r"[xX]?\d+", segments[-1]):
        segments = segments[:-1]
    if not segments:
        return None

    if segments[-1].endswith(".json"):
        stem = segments[-1][:-5]
        if stem in {"", "_index", "index"}:
            segments = segments[:-1]
        else:
            segments[-1] = stem
    if not segments:
        return None

    if segments[:3] == ["character", "blue-mage", "log"] and len(segments) >= 4:
        return "/".join(segments[:4])

    key = "/".join(segments[:3])
    return key or None


def _normalize_numeric_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        m = re.fullmatch(r"[xX]?(\d+)", raw)
        if m:
            return str(int(m.group(1)))
        return None
    return None


def default_completion_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata).expanduser() / "ffxiv-completionist" / "completion.json"


def list_detected_completion_files(limit: int = 20) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    default_path = default_completion_path()
    if default_path and default_path.exists() and default_path.is_file():
        out.append(default_path)
        seen.add(str(default_path).casefold())

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return out

    base = Path(appdata).expanduser() / "ffxiv-completionist"
    if not base.exists() or not base.is_dir():
        return out

    extra_patterns = (
        "completion*.json",
        "*completion*.json",
        "*.completion.json",
    )
    for pattern in extra_patterns:
        for candidate in sorted(base.glob(pattern), key=lambda p: p.name.lower()):
            if not candidate.exists() or not candidate.is_file():
                continue
            key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= limit:
                return out

    return out


def _resource_root_candidates() -> list[Path]:
    localapp = os.environ.get("LOCALAPPDATA")
    if not localapp:
        return []

    local_base = Path(localapp).expanduser()
    return [
        local_base / "Programs" / "ffxiv-completionist" / "resources" / "resources",
    ]


def resolve_resource_root() -> Path | None:
    for path in _resource_root_candidates():
        if path.exists() and path.is_dir():
            return path
    return None


def _resource_bucket_for_path(relative_path: str) -> str | None:
    path = "/" + relative_path.replace("\\", "/").casefold().strip("/")
    if "/logs/orchestrion-list/" in path:
        return "orchestrion"
    if "/social/emotes/" in path or "/social/emote/" in path:
        return "emote"
    if "/character/blue-mage/spellbook" in path:
        return "bluemagic"
    if "/gold-saucer/triple-triad-card-list/" in path:
        return "tripletriad"
    if "/character/achievement/" in path:
        return "achievement"
    if "/duty/quest/" in path:
        return "quest"
    if "/character/mount/" in path or path.endswith("/character/mount-guide.json"):
        return "mount"
    if "/character/minion/" in path or path.endswith("/character/minion-guide.json"):
        return "minion"
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    return _path_group_key(parts)


def _extract_labels(item: dict[str, Any]) -> list[str]:
    labels: list[str] = []

    def add_text(raw: str) -> None:
        text = raw.strip()
        if text and text not in labels and len(text) <= 180:
            labels.append(text)

    primary_fields = ("name_en", "title_en", "name", "title")
    for field in primary_fields:
        value = item.get(field)
        if isinstance(value, str):
            add_text(value)

    # Many desktop datasets use domain-specific English fields such as
    # mob_en, zone_en, source_en, etc.
    for field, value in item.items():
        if field in primary_fields:
            continue
        if not isinstance(value, str):
            continue
        if not field.casefold().endswith("_en"):
            continue
        add_text(value)

    return labels


def _extract_reference_ids(value: Any) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            m = _REFERENCE_ID_RE.fullmatch(node.strip())
            if m:
                refs.add((m.group(1).lower(), str(int(m.group(2)))))
            return
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if isinstance(node, dict):
            for child in node.values():
                visit(child)

    visit(value)
    return refs


@lru_cache(maxsize=4)
def _build_source_label_index(resource_root_str: str) -> dict[str, dict[str, tuple[str, ...]]]:
    resource_root = Path(resource_root_str)
    index: dict[str, dict[str, set[str]]] = {}
    refs_by_source: dict[tuple[str, str], set[tuple[str, str]]] = {}

    for json_path in resource_root.rglob("*.json"):
        try:
            relative = json_path.relative_to(resource_root).as_posix()
        except ValueError:
            continue
        bucket = _resource_bucket_for_path(relative)
        if bucket is None:
            continue

        try:
            doc = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue

        bucket_index = index.setdefault(bucket, {})

        tasks = doc.get("tasks")
        if isinstance(tasks, dict):
            for task_key, item in tasks.items():
                if not isinstance(item, dict):
                    continue
                id_key = _normalize_numeric_id(task_key)
                if id_key is None:
                    id_key = _normalize_numeric_id(item.get("id"))
                if id_key is None:
                    continue
                labels = _extract_labels(item)
                refs = _extract_reference_ids(item)
                if not labels and not refs:
                    continue
                label_set = bucket_index.setdefault(id_key, set())
                label_set.update(labels)
                if refs:
                    refs_by_source.setdefault((bucket, id_key), set()).update(refs)
            continue

        data = doc.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                id_key = _normalize_numeric_id(item.get("id"))
                if id_key is None:
                    continue
                labels = _extract_labels(item)
                refs = _extract_reference_ids(item)
                if not labels and not refs:
                    continue
                label_set = bucket_index.setdefault(id_key, set())
                label_set.update(labels)
                if refs:
                    refs_by_source.setdefault((bucket, id_key), set()).update(refs)

    # Resolve symbolic links (q.<id>, a.<id>, etc.) into concrete names so
    # buckets with tokenized labels still have workbook-matchable aliases.
    for (source_bucket, source_id), refs in refs_by_source.items():
        source_labels = index.get(source_bucket, {}).get(source_id)
        if source_labels is None:
            continue
        for prefix, ref_id in refs:
            target_bucket = _REFERENCE_BUCKET_BY_PREFIX.get(prefix)
            if not target_bucket:
                continue
            target_labels = index.get(target_bucket, {}).get(ref_id)
            if target_labels:
                source_labels.update(target_labels)

    frozen: dict[str, dict[str, tuple[str, ...]]] = {}
    for bucket, mapping in index.items():
        frozen[bucket] = {
            id_key: tuple(sorted(labels))
            for id_key, labels in mapping.items()
            if labels
        }
    return frozen


def _bucket_lookup_chain(bucket: str) -> list[str]:
    value = bucket.strip().strip("/")
    if not value:
        return []
    out = [value]
    while "/" in value:
        value = value.rsplit("/", 1)[0]
        out.append(value)
    return out


def _lookup_source_labels(
    source_index: dict[str, dict[str, tuple[str, ...]]],
    *,
    bucket: str,
    source_id: str,
) -> tuple[tuple[str, ...] | None, str | None]:
    for candidate_bucket in _bucket_lookup_chain(bucket):
        labels = source_index.get(candidate_bucket, {}).get(source_id)
        if labels:
            return labels, candidate_bucket

    for candidate_bucket in _CANONICAL_SOURCE_FALLBACKS.get(bucket, ()):
        labels = source_index.get(candidate_bucket, {}).get(source_id)
        if labels:
            return labels, candidate_bucket

    global_hits: list[tuple[str, tuple[str, ...]]] = []
    for candidate_bucket, bucket_values in source_index.items():
        labels = bucket_values.get(source_id)
        if labels:
            global_hits.append((candidate_bucket, labels))

    if len(global_hits) == 1:
        return global_hits[0][1], global_hits[0][0]
    return None, None


def _build_inline_completion_source_index(
    payload: dict[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Build id->label lookup from metadata embedded in completion.json.

    Desktop payloads can include custom-entry metadata under top-level
    ``custom`` (e.g. ``{"x804": {"name": "Turali Alumen"}}``). Those ids
    are present in ``overall.custom`` progress leaves, but are not represented
    in bundled resource JSON files. Without this fallback they become
    ``id_not_in_source_index`` and never reach workbook matching.
    """
    index: dict[str, dict[str, set[str]]] = {}

    custom = payload.get("custom")
    if isinstance(custom, dict):
        custom_idx = index.setdefault("custom", {})
        for raw_id, raw_item in custom.items():
            id_key = _normalize_numeric_id(raw_id)
            if id_key is None:
                continue

            labels: set[str] = set()
            if isinstance(raw_item, dict):
                labels.update(_extract_labels(raw_item))

                # Some desktop custom entries may use ad-hoc label fields.
                for field in ("label", "display_name"):
                    value = raw_item.get(field)
                    if isinstance(value, str):
                        text = value.strip()
                        if text:
                            labels.add(text)
            elif isinstance(raw_item, str):
                text = raw_item.strip()
                if text:
                    labels.add(text)

            if not labels:
                continue

            custom_idx.setdefault(id_key, set()).update(labels)

    frozen: dict[str, dict[str, tuple[str, ...]]] = {}
    for bucket, mapping in index.items():
        frozen[bucket] = {
            id_key: tuple(sorted(labels))
            for id_key, labels in mapping.items()
            if labels
        }
    return frozen


def _merge_source_indexes(
    base: dict[str, dict[str, tuple[str, ...]]],
    extra: dict[str, dict[str, tuple[str, ...]]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    if not extra:
        return base

    merged_sets: dict[str, dict[str, set[str]]] = {}

    for bucket, mapping in base.items():
        bucket_map = merged_sets.setdefault(bucket, {})
        for source_id, labels in mapping.items():
            bucket_map.setdefault(source_id, set()).update(
                label for label in labels if isinstance(label, str) and label.strip()
            )

    for bucket, mapping in extra.items():
        bucket_map = merged_sets.setdefault(bucket, {})
        for source_id, labels in mapping.items():
            bucket_map.setdefault(source_id, set()).update(
                label for label in labels if isinstance(label, str) and label.strip()
            )

    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for bucket, mapping in merged_sets.items():
        out[bucket] = {
            source_id: tuple(sorted(labels))
            for source_id, labels in mapping.items()
            if labels
        }
    return out


def _completion_bucket_from_path(path_parts: tuple[str, ...]) -> str | None:
    joined = "/" + "/".join(str(part).casefold() for part in path_parts if part)
    if "/logs/orchestrion-list/" in joined:
        return "orchestrion"
    if "/social/emotes/" in joined or "/social/emote/" in joined:
        return "emote"
    if "/character/blue-mage/spellbook/" in joined:
        return "bluemagic"
    if "triple-triad-card-list" in joined:
        return "tripletriad"
    if "/character/achievement/" in joined:
        return "achievement"
    if "/duty/quest/" in joined:
        return "quest"
    if "/mount-guide/" in joined or "/character/mount/" in joined:
        return "mount"
    if "/minion-guide/" in joined or "/character/minion/" in joined or "/adventure-plate/minion/" in joined:
        return "minion"
    return _path_group_key([str(part) for part in path_parts])


def _norm_lookup_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _norm_label(value))


def _bucket_tail(bucket: str) -> str:
    value = str(bucket or "").strip()
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def _parse_place_rank(label: str) -> tuple[str, int] | None:
    value = label.strip()
    if not value:
        return None

    m = re.match(r"^@PLACE\.([A-Z_']+)\s+(\d+)$", value, flags=re.IGNORECASE)
    if m:
        zone = m.group(1).replace("_", " ")
        rank = int(m.group(2))
        zone_key = _norm_lookup_key(zone)
        return _PLACE_ZONE_NORM_ALIASES.get(zone_key, zone_key), rank

    m = re.match(r"^([A-Za-z'\-\s]+)\s+(\d+)$", value)
    if m:
        zone = m.group(1).strip()
        rank = int(m.group(2))
        zone_key = _norm_lookup_key(zone)
        return _PLACE_ZONE_NORM_ALIASES.get(zone_key, zone_key), rank
    return None


def _parse_current_index(label: str) -> int | None:
    value = label.strip()
    if not value:
        return None

    patterns = (
        r"^@TRAVEL\.(?:COMPASS|QUEST)_CURRENT\s+(\d+)$",
        r"^(?:Compass|Quest|Aether)\s+Current\s+(\d+)$",
    )
    for pat in patterns:
        m = re.match(pat, value, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _aether_zone_from_path(path_parts: tuple[str, ...]) -> str | None:
    parts = [str(part).strip().casefold() for part in path_parts if str(part).strip()]
    for idx in range(len(parts) - 3):
        if parts[idx] == "travel" and parts[idx + 1] == "aether-currents":
            zone_raw = parts[idx + 3].replace("-", " ").replace("_", " ")
            return _norm_lookup_key(zone_raw)
    return None


def _build_blue_mage_log_position_index(
    resource_root: Path,
    blue_rows: list[tuple[str, int, str]],
) -> dict[tuple[str, str], list[tuple[str, int, str]]]:
    out: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    base = resource_root / "character" / "blue-mage" / "log"
    plan = (
        ("character/blue-mage/log/dungeon", "dungeon.json"),
        ("character/blue-mage/log/trial", "trial.json"),
        ("character/blue-mage/log/raid", "raid.json"),
    )

    ids_by_bucket: dict[str, list[str]] = {}
    for bucket, filename in plan:
        path = base / filename
        if not path.exists() or not path.is_file():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        tasks = doc.get("tasks") if isinstance(doc, dict) else None
        if not isinstance(tasks, dict):
            return {}
        task_ids = [
            (_normalize_numeric_id(key), key)
            for key in tasks.keys()
        ]
        normalized = [
            (norm_id, raw_key)
            for norm_id, raw_key in task_ids
            if norm_id is not None
        ]
        normalized.sort(key=lambda item: int(item[1].lstrip("xX")))
        ids_by_bucket[bucket] = [norm_id for norm_id, _ in normalized]

    expected = sum(len(v) for v in ids_by_bucket.values())
    if expected != len(blue_rows):
        return {}

    offset = 0
    for bucket, _filename in plan:
        source_ids = ids_by_bucket[bucket]
        for pos, source_id in enumerate(source_ids):
            row_entry = blue_rows[offset + pos]
            out[(bucket, source_id)] = [row_entry]
        offset += len(source_ids)
    return out


def _index_labels_for_global(
    *,
    node_label: str,
    row_json_obj: dict[str, Any] | None,
) -> set[str]:
    labels: set[str] = set()
    value = (node_label or "").strip()
    if value:
        labels.add(value)

    if not isinstance(row_json_obj, dict):
        return labels

    for field_value in row_json_obj.values():
        if isinstance(field_value, str):
            text = field_value.strip()
            if text and len(text) <= 160:
                labels.add(text)
            continue

        if isinstance(field_value, list):
            for item in field_value:
                if isinstance(item, str):
                    text = item.strip()
                    if text and len(text) <= 160:
                        labels.add(text)

    return labels


def _dedupe_hits(hits: list[tuple[str, int, str]] | None) -> list[tuple[str, int, str]] | None:
    if not hits:
        return hits
    out: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    for sheet_name, row_index, row_type in hits:
        item = (str(sheet_name), int(row_index), str(row_type))
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _partial_match_hits_generic(
    aliases: list[str],
    norm_index: dict[str, list[tuple[str, int, str]]],
    *,
    cutoff: float = 0.92,
) -> list[tuple[str, int, str]] | None:
    if not norm_index:
        return None

    norm_keys = list(norm_index.keys())
    for alias in aliases:
        norm = _norm_label(alias)
        if len(norm) < 8:
            continue
        close = difflib.get_close_matches(norm, norm_keys, n=1, cutoff=cutoff)
        if close:
            hits = norm_index.get(close[0])
            if hits:
                return hits
    return None


def _generic_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases: set[str] = {value}

    if value.startswith("@"):
        token_text = value[1:]
        token_text = re.sub(r"[._]+", " ", token_text)
        token_text = re.sub(r"\s+", " ", token_text).strip()
        if token_text:
            pretty = token_text.title()
            aliases.add(pretty)

            short = re.sub(
                r"^(PLACE|TRAVEL|SOCIETY|REPUTATION|EXPANSION|HEADER)\s+",
                "",
                token_text,
                flags=re.IGNORECASE,
            ).strip()
            if short:
                aliases.add(short.title())

            if " - " in token_text:
                left, right = [p.strip() for p in token_text.split(" - ", 1)]
                left = re.sub(
                    r"^(SOCIETY|PLACE|EXPANSION|TRAVEL)\s+",
                    "",
                    left,
                    flags=re.IGNORECASE,
                ).strip()
                right = re.sub(
                    r"^(REPUTATION|RANK)\s+",
                    "",
                    right,
                    flags=re.IGNORECASE,
                ).strip()
                if left and right:
                    aliases.add(f"{left.title()} {right.title()}")
                if left:
                    aliases.add(left.title())
                if right:
                    aliases.add(right.title())

    if " - " in value:
        right = value.split(" - ", 1)[1].strip()
        if right:
            aliases.add(right)

    if re.search(r"(?i)compass current|quest current", value):
        aliases.add(re.sub(r"(?i)compass current|quest current", "Aether Current", value))

    m_duty = re.fullmatch(r"[dD]\.(\d+)", value)
    if m_duty:
        duty_num = str(int(m_duty.group(1)))
        aliases.add(f"Duty {duty_num}")
        aliases.add(duty_num)

    return {alias for alias in aliases if alias.strip()}


def _decode_completion_value(raw: Any) -> tuple[str, float | None] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if text == "Y":
            return ("done", None)
        if text == "X":
            return ("excluded", None)
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            pct = max(0.0, float(text))
            return ("value", pct)
        return None

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        pct = max(0.0, float(raw))
        return ("value", pct)

    return None


def _walk_leaves(node: Any, path: tuple[str, ...] = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield from _walk_leaves(value, path + (key,))
    else:
        yield path, node


def _merge_source_state(
    current_state: str,
    current_value: float | None,
    new_state: str,
    new_value: float | None,
) -> tuple[str, float | None]:
    priority = {"excluded": 1, "value": 2, "done": 3}
    if priority[new_state] > priority[current_state]:
        return new_state, new_value
    if priority[new_state] < priority[current_state]:
        return current_state, current_value
    if new_state == "value":
        return "value", max(float(current_value or 0.0), float(new_value or 0.0))
    return current_state, current_value


def _merge_row_action(
    row_actions: dict[tuple[str, int], dict[str, Any]],
    key: tuple[str, int],
    *,
    row_type: str,
    state: str,
    value: float | None,
) -> None:
    existing = row_actions.get(key)
    if existing is None:
        row_actions[key] = {
            "row_type": row_type,
            "state": state,
            "value": value,
        }
        return

    merged_state, merged_value = _merge_source_state(
        str(existing.get("state") or "excluded"),
        existing.get("value") if isinstance(existing.get("value"), (int, float)) else None,
        state,
        value,
    )
    existing["state"] = merged_state
    existing["value"] = merged_value
    existing["row_type"] = row_type


def load_completion_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Desktop completion payload must be a JSON object")
    overall = data.get("overall")
    if not isinstance(overall, dict):
        raise ValueError("Desktop completion payload is missing an object 'overall' section")
    return data


def import_desktop_completion(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    completion_path: Path,
    clear_existing: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ImportSummary:
    def log(message: str) -> None:
        if progress is not None:
            progress(message)

    run_id = db.latest_run_id(conn)
    if run_id is None:
        raise ValueError("No ingest run found. Run scripts/prep_xlsx_to_sqlite.py first.")

    character = db.get_character(conn, character_id)
    if character is None:
        raise ValueError(f"Character id {character_id} was not found")

    log(f"Loading desktop completion payload: {completion_path}")
    payload = load_completion_payload(completion_path)

    resource_root = resolve_resource_root()
    if resource_root is None:
        raise ValueError(
            "Could not find desktop app resources under LOCALAPPDATA. "
            "Expected LOCALAPPDATA/Programs/ffxiv-completionist/resources/resources"
        )

    source_index = _build_source_label_index(str(resource_root))

    inline_source_index = _build_inline_completion_source_index(payload)
    if inline_source_index:
        source_index = _merge_source_indexes(source_index, inline_source_index)

    indexed_count = sum(len(v) for v in source_index.values())
    log(f"Loaded desktop source index from {resource_root} ({indexed_count} ids)")
    if inline_source_index:
        inline_count = sum(len(v) for v in inline_source_index.values())
        log(f"Merged {inline_count} inline source ids from completion payload metadata")

    if clear_existing:
        log("Clearing existing character progress before import")
        reset_character_progress(conn, character, run_id)

    # Aggregate by source id so duplicate leaves in alternate branches collapse deterministically.
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    missing_source_ids: dict[tuple[str, str], dict[str, Any]] = {}
    supported_entries = 0

    for path_parts, raw_value in _walk_leaves(payload.get("overall", {}), ("overall",)):
        if not path_parts:
            continue
        leaf_id = _normalize_numeric_id(path_parts[-1])
        if leaf_id is None:
            continue

        bucket = _completion_bucket_from_path(path_parts)
        if bucket is None:
            continue

        state_info = _decode_completion_value(raw_value)
        if state_info is None:
            continue

        supported_entries += 1
        state, pct = state_info
        labels, source_bucket = _lookup_source_labels(
            source_index,
            bucket=bucket,
            source_id=leaf_id,
        )
        if not labels and _bucket_tail(bucket) not in _POSITIONAL_VALUE_BUCKETS:
            missing_key = (bucket, leaf_id)
            existing_missing = missing_source_ids.get(missing_key)
            if existing_missing is None:
                missing_source_ids[missing_key] = {
                    "bucket": bucket,
                    "label": f"id:{leaf_id}",
                    "source_id": leaf_id,
                    "source_state": state,
                    "reason": "id_not_in_source_index",
                    "value": pct,
                }
            else:
                merged_state, merged_value = _merge_source_state(
                    str(existing_missing.get("source_state") or "excluded"),
                    (
                        float(existing_missing["value"])
                        if isinstance(existing_missing.get("value"), (int, float))
                        else None
                    ),
                    state,
                    pct,
                )
                existing_missing["source_state"] = merged_state
                existing_missing["value"] = merged_value
            continue

        key = (bucket, leaf_id)
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = {
                "bucket": bucket,
                "source_bucket": source_bucket,
                "source_id": leaf_id,
                "source_state": state,
                "value": pct,
                "labels": list(labels) if labels else [],
                "source_path_parts": [str(part) for part in path_parts],
            }
            continue

        merged_state, merged_value = _merge_source_state(
            str(existing.get("source_state") or "excluded"),
            existing.get("value") if isinstance(existing.get("value"), (int, float)) else None,
            state,
            pct,
        )
        existing["source_state"] = merged_state
        existing["value"] = merged_value
        label_pool = {str(label).strip() for label in existing.get("labels", []) if isinstance(label, str)}
        label_pool.update(str(label).strip() for label in (labels or ()) if isinstance(label, str))
        existing["labels"] = sorted(label for label in label_pool if label)
        if not existing.get("source_bucket") and source_bucket:
            existing["source_bucket"] = source_bucket
        if not existing.get("source_path_parts"):
            existing["source_path_parts"] = [str(part) for part in path_parts]

    candidates = list(aggregated.values())
    unmatched_items: list[dict[str, Any]] = [
        {
            "bucket": item["bucket"],
            "label": item["label"],
            "source_id": item["source_id"],
            "source_state": item["source_state"],
            "reason": item["reason"],
        }
        for item in missing_source_ids.values()
    ]
    total_candidates = len(candidates) + len(unmatched_items)
    log(
        f"Collected {total_candidates} desktop candidates from {supported_entries} supported completion entries"
    )

    rows = conn.execute(
        """
                SELECT n.sheet_name, n.row_index, n.section_label, n.label, n.row_type, n.row_json
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        WHERE n.run_id = ?
          AND s.is_menu = 0
          AND n.label IS NOT NULL
          AND n.row_type IN ('checkbox', 'value')
        """,
        (run_id,),
    ).fetchall()

    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    global_exact_idx: dict[str, list[tuple[str, int, str]]] = {}
    global_norm_idx: dict[str, list[tuple[str, int, str]]] = {}
    shared_fate_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    aether_current_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    blue_mage_rows: list[tuple[str, int, str]] = []
    classes_jobs_rows: list[tuple[str, int, str]] = []
    desynthesis_rows: list[tuple[str, int, str]] = []

    for row in rows:
        sheet_name = row["sheet_name"]
        label = (row["label"] or "").strip()
        if not label:
            continue

        row_json_obj: dict[str, Any] | None = None
        row_json_text = row["row_json"]
        if isinstance(row_json_text, str) and row_json_text.strip():
            try:
                decoded = json.loads(row_json_text)
                if isinstance(decoded, dict):
                    row_json_obj = decoded
            except json.JSONDecodeError:
                row_json_obj = None

        entry = (sheet_name, int(row["row_index"]), row["row_type"])
        section_label = str(row["section_label"] or "")

        if sheet_name == "Shared FATE":
            section_key = _norm_lookup_key(section_label)
            rank_value: int | None = None
            rank_from_label = _normalize_numeric_id(label)
            if rank_from_label is not None:
                rank_value = int(rank_from_label)
            elif isinstance(row_json_obj, dict):
                rank_from_json = _normalize_numeric_id(row_json_obj.get("rank"))
                if rank_from_json is not None:
                    rank_value = int(rank_from_json)
            if section_key and rank_value is not None:
                shared_fate_idx.setdefault((section_key, rank_value), []).append(entry)

        if sheet_name == "Aether Currents" and isinstance(row_json_obj, dict):
            section_key = _norm_lookup_key(section_label)
            current_value = _normalize_numeric_id(row_json_obj.get("col_2"))
            if section_key and current_value is not None:
                aether_current_idx.setdefault((section_key, int(current_value)), []).append(entry)

        if sheet_name == "Blue Mage Log":
            blue_mage_rows.append(entry)

        if sheet_name == "Classes-Jobs" and row["row_type"] == "value":
            section_norm = _norm_label(section_label)
            if "desynthesis" in section_norm:
                desynthesis_rows.append(entry)
            else:
                classes_jobs_rows.append(entry)

        for idx_label in _index_labels_for_global(
            node_label=label,
            row_json_obj=row_json_obj,
        ):
            global_exact_idx.setdefault(idx_label.casefold(), []).append(entry)
            norm = _norm_label(idx_label)
            if norm:
                global_norm_idx.setdefault(norm, []).append(entry)

        for bucket in _sheet_buckets(sheet_name):
            for idx_label in _index_labels_for_bucket(
                bucket=bucket,
                node_label=label,
                row_json_obj=row_json_obj,
            ):
                exact_idx.setdefault(bucket, {}).setdefault(idx_label.casefold(), []).append(entry)
                norm = _norm_label(idx_label)
                if norm:
                    norm_idx.setdefault(bucket, {}).setdefault(norm, []).append(entry)

    norm_keys_idx: dict[str, list[str]] = {
        bucket: list(bucket_norm.keys())
        for bucket, bucket_norm in norm_idx.items()
    }
    blue_mage_rows.sort(key=lambda item: item[1])
    classes_jobs_rows.sort(key=lambda item: item[1])
    desynthesis_rows.sort(key=lambda item: item[1])
    blue_mage_idx = _build_blue_mage_log_position_index(resource_root, blue_mage_rows)
    classes_jobs_idx: dict[str, list[tuple[str, int, str]]] = {
        str(pos): [entry] for pos, entry in enumerate(classes_jobs_rows)
    }
    desynthesis_idx: dict[str, list[tuple[str, int, str]]] = {
        str(pos): [entry] for pos, entry in enumerate(desynthesis_rows)
    }

    row_actions: dict[tuple[str, int], dict[str, Any]] = {}
    matched_candidates = 0
    ignored_untracked = 0

    for candidate in candidates:
        bucket = str(candidate.get("bucket") or "")
        source_id = str(candidate.get("source_id") or "")
        source_path_parts = tuple(str(part) for part in candidate.get("source_path_parts") or ())
        source_state = str(candidate.get("source_state") or "done")
        source_value = (
            float(candidate["value"])
            if isinstance(candidate.get("value"), (int, float))
            else None
        )

        labels = [
            str(label).strip()
            for label in candidate.get("labels", [])
            if isinstance(label, str) and str(label).strip()
        ]
        bucket_tail = _bucket_tail(bucket)
        if not labels and bucket_tail not in _POSITIONAL_VALUE_BUCKETS:
            unmatched_items.append({
                "bucket": bucket,
                "label": f"id:{candidate.get('source_id')}",
                "source_id": candidate.get("source_id"),
                "source_state": source_state,
                "reason": "missing_source_labels",
            })
            continue

        hits: list[tuple[str, int, str]] | None = None

        if bucket_tail == "classes-jobs":
            hits = classes_jobs_idx.get(source_id)
        elif bucket_tail == "desynthesis":
            hits = desynthesis_idx.get(source_id)

        if bucket.startswith("character/blue-mage/log/"):
            hits = blue_mage_idx.get((bucket, source_id))

        if not hits and bucket.startswith("travel/shared-fate/"):
            for source_label in labels:
                parsed = _parse_place_rank(source_label)
                if not parsed:
                    continue
                zone_key, rank_value = parsed
                hits = shared_fate_idx.get((zone_key, rank_value))
                if hits:
                    break

        if not hits and bucket.startswith("travel/aether-currents/"):
            zone_key = _aether_zone_from_path(source_path_parts)
            if zone_key:
                for source_label in labels:
                    current_value = _parse_current_index(source_label)
                    if current_value is None:
                        continue
                    hits = aether_current_idx.get((zone_key, current_value))
                    if hits:
                        break

        aliases: list[str] = []
        if not hits:
            seen_aliases: set[str] = set()
            for source_label in labels:
                for base_alias in _candidate_aliases(bucket, source_label):
                    expanded = _generic_label_aliases(base_alias)
                    ordered_aliases = [base_alias] + sorted(
                        alias for alias in expanded if alias != base_alias
                    )
                    for alias in ordered_aliases:
                        key = alias.casefold()
                        if key in seen_aliases:
                            continue
                        seen_aliases.add(key)
                        aliases.append(alias)

            bucket_exact = exact_idx.get(bucket, {})
            bucket_norm = norm_idx.get(bucket, {})

            for alias in aliases:
                hits = bucket_exact.get(alias.casefold())
                if hits:
                    break
                norm = _norm_label(alias)
                hits = bucket_norm.get(norm)
                if hits:
                    break

            if not hits:
                hits = _partial_match_hits(
                    bucket=bucket,
                    aliases=aliases,
                    bucket_norm=bucket_norm,
                    bucket_norm_keys=norm_keys_idx.get(bucket, []),
                )

            if not hits:
                for alias in aliases:
                    hits = global_exact_idx.get(alias.casefold())
                    if hits:
                        break
                    norm = _norm_label(alias)
                    hits = global_norm_idx.get(norm)
                    if hits:
                        break

            if not hits:
                hits = _partial_match_hits_generic(aliases, global_norm_idx, cutoff=0.92)

        hits = _dedupe_hits(hits)

        if not hits:
            if bucket in _IGNORED_UNTRACKED_BUCKETS:
                ignored_untracked += 1
                continue
            primary_label = labels[0]
            reason, reason_extra = _unmatched_reason(
                bucket=bucket,
                raw_label=primary_label,
                aliases=aliases,
                exact_idx=exact_idx,
                norm_idx=norm_idx,
            )
            unmatched_item = {
                "bucket": bucket,
                "label": primary_label,
                "source_id": candidate.get("source_id"),
                "source_state": source_state,
                "reason": reason,
                "attempted_aliases": aliases[:6],
            }
            unmatched_item.update(reason_extra)
            unmatched_items.append(unmatched_item)
            continue

        matched_candidates += 1
        for sheet_name, row_index, row_type in hits:
            _merge_row_action(
                row_actions,
                (sheet_name, row_index),
                row_type=row_type,
                state=source_state,
                value=source_value,
            )

    log(
        f"Matched {matched_candidates}/{total_candidates} candidates; "
        f"resolved to {len(row_actions)} workbook rows"
    )

    rows_applied = 0
    rows_skipped = 0
    starting_class = character["starting_class"]

    with progress_io.batch(conn, character_id):
        ordered_targets = sorted(row_actions.items(), key=lambda item: (item[0][0], item[0][1]))
        for idx, ((sheet_name, row_index), action) in enumerate(ordered_targets, start=1):
            row_type = str(action.get("row_type") or "checkbox")
            state = str(action.get("state") or "done")
            raw_value = action.get("value")
            value = float(raw_value) if isinstance(raw_value, (int, float)) else None

            current = db.effective_state(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
                starting_class,
            )

            if state == "done":
                if current == "done":
                    rows_skipped += 1
                    continue
                if row_type == "value":
                    target_value = value
                    if target_value is None:
                        target_value = db.value_row_cap(conn, run_id, sheet_name, row_index)
                    db.set_row_value(
                        conn,
                        character_id,
                        run_id,
                        sheet_name,
                        row_index,
                        target_value,
                        commit=False,
                        starting_class=starting_class,
                    )
                else:
                    db.set_row_state(
                        conn,
                        character_id,
                        run_id,
                        sheet_name,
                        row_index,
                        "done",
                        commit=False,
                        starting_class=starting_class,
                    )
                rows_applied += 1
                continue

            if state == "excluded":
                if current == "excluded":
                    rows_skipped += 1
                    continue
                db.set_row_state(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    "excluded",
                    commit=False,
                    starting_class=starting_class,
                )
                rows_applied += 1
                continue

            # Numeric values map to value rows directly; checkbox rows only
            # become done when the source value reaches 100%.
            if row_type == "value":
                target_value = value if value is not None else 0.0
                cap = db.value_row_cap(conn, run_id, sheet_name, row_index)
                desired_state = "done" if target_value >= cap else "todo"
                if desired_state == "done" and current == "done":
                    rows_skipped += 1
                    continue
                db.set_row_value(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    target_value,
                    commit=False,
                    starting_class=starting_class,
                )
                rows_applied += 1
            else:
                target_value = value if value is not None else 0.0
                if target_value >= 100.0:
                    if current == "done":
                        rows_skipped += 1
                        continue
                    db.set_row_state(
                        conn,
                        character_id,
                        run_id,
                        sheet_name,
                        row_index,
                        "done",
                        commit=False,
                        starting_class=starting_class,
                    )
                    rows_applied += 1
                else:
                    rows_skipped += 1

            if idx % 60 == 0:
                log(f"Applied {idx}/{len(ordered_targets)} mapped rows")

    conn.commit()

    unmatched_candidates = len(unmatched_items)
    log(
        f"Desktop import complete: applied={rows_applied}, already_set={rows_skipped}, "
        f"unmatched_candidates={unmatched_candidates}"
    )
    if unmatched_items:
        reason_counts = Counter(str(item.get("reason") or "unknown") for item in unmatched_items)
        reason_msg = ", ".join(
            f"{reason}={count}" for reason, count in sorted(reason_counts.items())
        )
        log(f"Unmatched reasons: {reason_msg}")
    if ignored_untracked:
        log(f"Skipped {ignored_untracked} known-untracked desktop entries")

    return ImportSummary(
        character_id=character_id,
        character_name=character["name"],
        source_path=str(completion_path),
        run_id=run_id,
        total_candidates=total_candidates,
        matched_candidates=matched_candidates,
        unmatched_candidates=unmatched_candidates,
        rows_applied=rows_applied,
        rows_skipped_already_done=rows_skipped,
        unmatched_items=unmatched_items,
    )
