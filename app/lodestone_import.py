from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import sqlite3
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from collections import Counter

from app import db, progress_io


_COMMON_CONFUSABLES = str.maketrans({
    "§": "s",
    "Α": "A",
    "α": "a",
    "Ι": "I",
    "ι": "i",
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


def _sheet_buckets(sheet_name: str) -> set[str]:
    name = sheet_name.lower()
    out: set[str] = set()

    # Some raid/side-story sheets track questline completion but don't include
    # the word "quest" in the sheet title.
    quest_like_tokens = (
        "quest",
        "alexander",
        "bahamut",
        "primals",
        "warring triad",
        "hildibrand",
        "crystal tower",
        "shadow of mach",
        "records of unusual endeavors",
        "chronicles of light",
        "omega",
        "yorha",
        "eden",
        "ivalice",
        "eureka",
        "weapon enhancement",
    )
    if any(token in name for token in quest_like_tokens):
        out.add("quest")
    if "achievement" in name or "achiev" in name:
        out.add("achievement")
    if "minion" in name:
        out.add("minion")
    if "mount" in name:
        out.add("mount")
    if "triple triad" in name or "card" in name:
        out.add("tripletriad")
    if "blue magic spellbook" in name:
        out.add("bluemagic")
    if "emote" in name:
        out.add("emote")
    if "orchestrion" in name:
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


def import_lodestone_payload(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    payload_path: Path,
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

    log(f"Loading payload: {payload_path}")
    payload = load_payload(payload_path)
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
