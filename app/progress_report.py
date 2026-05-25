"""Between-run/import progress snapshots and diff reports.

This module persists baseline snapshots and produces actionable diff reports.
Reports can include line-level review items that users resolve from the web UI.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from app import db, progress_io

SCHEMA_VERSION = "ffxiv-tracker/progress-between-run/v2"
BASELINE_FILE_NAME = "progress_baseline.json"
LATEST_REPORT_FILE_NAME = "latest.json"
SAMPLE_LIMIT_DEFAULT = 40
RESOLUTION_VALUES = {"done", "excluded", "todo"}
MAX_PERSISTED_BETWEEN_RUN_REPORTS = 10


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _report_root() -> Path:
    return progress_io.PROGRESS_DIR.parent / "logs" / "progress_reports"


def baseline_path() -> Path:
    return _report_root() / BASELINE_FILE_NAME


def latest_report_path() -> Path:
    return _report_root() / LATEST_REPORT_FILE_NAME


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _normalize_progress_value(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, 6)


def _normalize_state(raw: Any) -> str:
    state = str(raw or "todo").strip().lower()
    if state not in {"done", "todo", "excluded"}:
        return "todo"
    return state


def _row_identity_key(row: sqlite3.Row) -> str:
    ids = progress_io.compute_stable_ids(
        str(row["sheet_name"]),
        row["section_label"],
        row["label"],
        row["row_json"],
        int(row["row_index"]),
    )
    return (
        ids.get("hash")
        or ids.get("section_label")
        or ids.get("label")
        or ids["position"]
    )


def _entry_fingerprint(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        value = entry.get("v")
        value_text = "" if value is None else f"{float(value):.6f}"
        lines.append(
            "|".join(
                [
                    str(entry.get("k") or ""),
                    str(entry.get("s") or "todo"),
                    value_text,
                    str(entry.get("sheet_name") or ""),
                    str(entry.get("row_index") or ""),
                ]
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _character_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    name: str,
    starting_class: str | None,
) -> dict[str, Any]:
    rows = db.fetch_export_rows(conn, run_id, character_id, starting_class)
    counts = {
        "done": 0,
        "todo": 0,
        "excluded": 0,
        "total": len(rows),
    }

    base_key_seen: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    active_entries = 0

    for row in rows:
        state = _normalize_state(row["state"])
        counts[state] += 1
        value = _normalize_progress_value(row["progress_percent"])

        if state != "todo" or value is not None:
            active_entries += 1

        base_key = _row_identity_key(row)
        ordinal = base_key_seen.get(base_key, 0) + 1
        base_key_seen[base_key] = ordinal
        entry_key = f"{base_key}#{ordinal}"

        entries.append(
            {
                "k": entry_key,
                "sheet_name": str(row["sheet_name"]),
                "row_index": int(row["row_index"]),
                "row_type": str(row["row_type"] or "checkbox"),
                "section_label": str(row["section_label"] or ""),
                "label": str(row["label"] or ""),
                "s": state,
                "v": value,
            }
        )

    entries.sort(key=lambda item: str(item.get("k") or ""))

    return {
        "character_id": character_id,
        "name": name,
        "starting_class": starting_class,
        "counts": counts,
        "active_entries": active_entries,
        "entry_count": len(entries),
        "fingerprint": _entry_fingerprint(entries),
        "entries": entries,
    }


def _run_snapshot_metadata(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, source_file, started_at, completed_at, sheet_count, row_count
        FROM ingest_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return {"id": run_id}
    return {
        "id": int(row["id"]),
        "source_file": str(row["source_file"] or ""),
        "started_at": str(row["started_at"] or ""),
        "completed_at": str(row["completed_at"] or ""),
        "sheet_count": int(row["sheet_count"] or 0),
        "row_count": int(row["row_count"] or 0),
    }


def build_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    source: str,
    run_token: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    chars = conn.execute(
        """
        SELECT id, name, starting_class
        FROM characters
        WHERE name IS NOT NULL
        ORDER BY LOWER(name), id
        """
    ).fetchall()

    character_docs: list[dict[str, Any]] = []
    total_counts = {
        "done": 0,
        "todo": 0,
        "excluded": 0,
        "total": 0,
        "active_entries": 0,
        "entry_count": 0,
    }

    for char in chars:
        name = str(char["name"] or "").strip()
        if not name:
            continue
        character_doc = _character_snapshot(
            conn,
            run_id,
            int(char["id"]),
            name,
            str(char["starting_class"]) if char["starting_class"] is not None else None,
        )
        character_docs.append(character_doc)
        counts = character_doc.get("counts", {})
        total_counts["done"] += int(counts.get("done") or 0)
        total_counts["todo"] += int(counts.get("todo") or 0)
        total_counts["excluded"] += int(counts.get("excluded") or 0)
        total_counts["total"] += int(counts.get("total") or 0)
        total_counts["active_entries"] += int(character_doc.get("active_entries") or 0)
        total_counts["entry_count"] += int(character_doc.get("entry_count") or 0)

    return {
        "schema": SCHEMA_VERSION,
        "captured_at": _now_iso(),
        "source": source,
        "run": _run_snapshot_metadata(conn, run_id),
        "run_token": list(run_token) if run_token is not None else None,
        "totals": total_counts,
        "characters": character_docs,
    }


def load_baseline_snapshot() -> dict[str, Any] | None:
    return _read_json(baseline_path())


def save_baseline_snapshot(snapshot: dict[str, Any]) -> Path:
    path = baseline_path()
    _write_json(path, snapshot)
    return path


def save_current_baseline(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    source: str,
    run_token: tuple[Any, ...] | None = None,
) -> Path:
    snapshot = build_snapshot(
        conn,
        run_id,
        source=source,
        run_token=run_token,
    )
    return save_baseline_snapshot(snapshot)


def load_latest_report() -> dict[str, Any] | None:
    return _read_json(latest_report_path())


def _character_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    chars = snapshot.get("characters")
    if not isinstance(chars, list):
        return out
    for char in chars:
        if not isinstance(char, dict):
            continue
        name = str(char.get("name") or "").strip()
        if not name:
            continue
        out[name.casefold()] = char
    return out


def _entry_map(character: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    raw_entries = character.get("entries")
    if not isinstance(raw_entries, list):
        return out
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        key = str(item.get("k") or "")
        if not key:
            continue
        out[key] = {
            "key": key,
            "state": _normalize_state(item.get("s")),
            "value": _normalize_progress_value(item.get("v")),
            "sheet_name": str(item.get("sheet_name") or ""),
            "row_index": int(item.get("row_index") or 0),
            "row_type": str(item.get("row_type") or "checkbox"),
            "section_label": str(item.get("section_label") or ""),
            "label": str(item.get("label") or ""),
        }
    return out


def _review_item_id(character_id: int | None, key: str) -> str:
    cid = int(character_id or 0)
    digest = hashlib.sha256(f"{cid}|{key}".encode("utf-8")).hexdigest()[:16]
    return f"ri_{cid}_{digest}"


def _sample_entries(
    keys: list[str],
    entries: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    sampled: list[dict[str, Any]] = []
    for key in keys[: max(0, limit)]:
        value = entries.get(key, {})
        sampled.append(
            {
                "key": key,
                "sheet_name": value.get("sheet_name"),
                "row_index": value.get("row_index"),
                "row_type": value.get("row_type"),
                "label": value.get("label"),
                "section_label": value.get("section_label"),
                "state": value.get("state"),
                "value": value.get("value"),
            }
        )
    return sampled


def _blank_resolution() -> dict[str, Any]:
    return {
        "status": "todo",
        "updated_at": "",
        "applied": False,
        "applied_state": None,
    }


def _normalize_review_resolution(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _blank_resolution()
    status = str(raw.get("status") or "todo").strip().lower()
    if status not in RESOLUTION_VALUES:
        status = "todo"
    out = {
        "status": status,
        "updated_at": str(raw.get("updated_at") or ""),
        "applied": bool(raw.get("applied")) if status != "todo" else False,
        "applied_state": raw.get("applied_state"),
    }
    return out


def _summarize_review_items(review_items: list[dict[str, Any]]) -> dict[str, int]:
    unresolved = 0
    accepted = 0
    reverted = 0
    for item in review_items:
        resolution = _normalize_review_resolution(item.get("resolution"))
        status = resolution["status"]
        if status == "todo":
            unresolved += 1
        elif status == "done":
            accepted += 1
        elif status == "excluded":
            reverted += 1
    return {
        "unresolved": unresolved,
        "accepted": accepted,
        "reverted": reverted,
        "total": len(review_items),
    }


def count_unresolved_review_items(
    report_doc: dict[str, Any],
    *,
    character_id: int | None = None,
) -> int:
    review_items = report_doc.get("review_items")
    if not isinstance(review_items, list):
        return 0
    unresolved = 0
    for item in review_items:
        if not isinstance(item, dict):
            continue
        if character_id is not None:
            try:
                item_character_id = int(item.get("character_id") or 0)
            except (TypeError, ValueError):
                item_character_id = 0
            if item_character_id != int(character_id):
                continue
        resolution = _normalize_review_resolution(item.get("resolution"))
        if resolution["status"] == "todo":
            unresolved += 1
    return unresolved


def review_items_for_character(
    report_doc: dict[str, Any],
    character_id: int,
) -> list[dict[str, Any]]:
    review_items = report_doc.get("review_items")
    if not isinstance(review_items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in review_items:
        if not isinstance(item, dict):
            continue
        try:
            item_character_id = int(item.get("character_id") or 0)
        except (TypeError, ValueError):
            continue
        if item_character_id != int(character_id):
            continue
        clone = dict(item)
        clone["resolution"] = _normalize_review_resolution(item.get("resolution"))
        out.append(clone)
    return out


def set_review_item_resolution(
    report_doc: dict[str, Any],
    *,
    item_id: str,
    status: str,
    applied_state: str | None = None,
) -> dict[str, Any] | None:
    review_items = report_doc.get("review_items")
    if not isinstance(review_items, list):
        return None

    normalized_status = str(status or "todo").strip().lower()
    if normalized_status not in RESOLUTION_VALUES:
        return None

    for item in review_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "") != item_id:
            continue
        resolution = _normalize_review_resolution(item.get("resolution"))
        resolution["status"] = normalized_status
        resolution["updated_at"] = _now_iso()
        resolution["applied"] = normalized_status != "todo"
        resolution["applied_state"] = applied_state if normalized_status != "todo" else None
        item["resolution"] = resolution

        review_summary = _summarize_review_items(review_items)
        summary = report_doc.get("summary")
        if isinstance(summary, dict):
            summary["review_unresolved"] = review_summary["unresolved"]
            summary["review_resolved_done"] = review_summary["accepted"]
            summary["review_resolved_excluded"] = review_summary["reverted"]
            summary["review_total"] = review_summary["total"]
        return item
    return None


def save_report_document(report_doc: dict[str, Any], report_path: Path | None = None) -> Path:
    path = report_path
    if path is None:
        raw = report_doc.get("report_path")
        if isinstance(raw, str) and raw.strip():
            path = Path(raw)
    if path is None:
        path = _next_report_path()

    report_doc["report_path"] = str(path)
    _write_json(path, report_doc)
    _write_json(latest_report_path(), report_doc)
    return path


def compare_snapshots(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    sample_limit: int = SAMPLE_LIMIT_DEFAULT,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_chars = _character_index(baseline)
    current_chars = _character_index(current)

    all_names = sorted(set(baseline_chars) | set(current_chars))
    character_rows: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    advanced_items: list[dict[str, Any]] = []

    summary = {
        "baseline_available": True,
        "characters_compared": 0,
        "characters_changed": 0,
        "characters_added": 0,
        "characters_removed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "entries_changed": 0,
    }

    for name_key in all_names:
        before = baseline_chars.get(name_key)
        after = current_chars.get(name_key)

        if before is None and after is not None:
            summary["characters_added"] += 1
            summary["characters_changed"] += 1
            after_counts = after.get("counts") if isinstance(after, dict) else {}
            after_entries = _entry_map(after)
            for key in sorted(after_entries):
                entry = after_entries[key]
                advanced_items.append(
                    {
                        "type": "added",
                        "character_id": int(after.get("character_id") or 0),
                        "character_name": str(after.get("name") or ""),
                        "entry": entry,
                    }
                )
            character_rows.append(
                {
                    "name": str(after.get("name") or ""),
                    "character_id": int(after.get("character_id") or 0),
                    "status": "added",
                    "changed": True,
                    "before": None,
                    "after": {
                        "counts": after_counts,
                        "active_entries": int(after.get("active_entries") or 0),
                        "fingerprint": str(after.get("fingerprint") or ""),
                    },
                    "delta": {
                        "done": int((after_counts or {}).get("done") or 0),
                        "todo": int((after_counts or {}).get("todo") or 0),
                        "excluded": int((after_counts or {}).get("excluded") or 0),
                        "total": int((after_counts or {}).get("total") or 0),
                    },
                    "entries": {
                        "added_count": int(after.get("entry_count") or 0),
                        "removed_count": 0,
                        "changed_count": 0,
                        "added_sample": _sample_entries(
                            sorted(after_entries.keys()),
                            after_entries,
                            limit=sample_limit,
                        ),
                        "removed_sample": [],
                        "changed_sample": [],
                    },
                }
            )
            continue

        if after is None and before is not None:
            summary["characters_removed"] += 1
            summary["characters_changed"] += 1
            before_counts = before.get("counts") if isinstance(before, dict) else {}
            before_entries = _entry_map(before)
            for key in sorted(before_entries):
                entry = before_entries[key]
                advanced_items.append(
                    {
                        "type": "removed",
                        "character_id": int(before.get("character_id") or 0),
                        "character_name": str(before.get("name") or ""),
                        "entry": entry,
                    }
                )
            character_rows.append(
                {
                    "name": str(before.get("name") or ""),
                    "character_id": int(before.get("character_id") or 0),
                    "status": "removed",
                    "changed": True,
                    "before": {
                        "counts": before_counts,
                        "active_entries": int(before.get("active_entries") or 0),
                        "fingerprint": str(before.get("fingerprint") or ""),
                    },
                    "after": None,
                    "delta": {
                        "done": -int((before_counts or {}).get("done") or 0),
                        "todo": -int((before_counts or {}).get("todo") or 0),
                        "excluded": -int((before_counts or {}).get("excluded") or 0),
                        "total": -int((before_counts or {}).get("total") or 0),
                    },
                    "entries": {
                        "added_count": 0,
                        "removed_count": int(before.get("entry_count") or 0),
                        "changed_count": 0,
                        "added_sample": [],
                        "removed_sample": _sample_entries(
                            sorted(before_entries.keys()),
                            before_entries,
                            limit=sample_limit,
                        ),
                        "changed_sample": [],
                    },
                }
            )
            continue

        assert before is not None and after is not None
        summary["characters_compared"] += 1

        before_counts = before.get("counts") if isinstance(before, dict) else {}
        after_counts = after.get("counts") if isinstance(after, dict) else {}

        before_entries = _entry_map(before)
        after_entries = _entry_map(after)

        before_keys = set(before_entries)
        after_keys = set(after_entries)
        added_keys = sorted(after_keys - before_keys)
        removed_keys = sorted(before_keys - after_keys)

        changed_keys: list[str] = []
        for key in sorted(before_keys & after_keys):
            if before_entries[key]["state"] != after_entries[key]["state"] or (
                before_entries[key]["value"] != after_entries[key]["value"]
            ):
                changed_keys.append(key)

        summary["entries_added"] += len(added_keys)
        summary["entries_removed"] += len(removed_keys)
        summary["entries_changed"] += len(changed_keys)

        for key in added_keys:
            advanced_items.append(
                {
                    "type": "added",
                    "character_id": int(after.get("character_id") or 0),
                    "character_name": str(after.get("name") or ""),
                    "entry": after_entries[key],
                }
            )
        for key in removed_keys:
            advanced_items.append(
                {
                    "type": "removed",
                    "character_id": int(before.get("character_id") or 0),
                    "character_name": str(before.get("name") or ""),
                    "entry": before_entries[key],
                }
            )

        changed_sample: list[dict[str, Any]] = []
        for key in changed_keys[: max(0, sample_limit)]:
            before_entry = before_entries[key]
            after_entry = after_entries[key]
            changed_sample.append(
                {
                    "key": key,
                    "before": {
                        "state": before_entry["state"],
                        "value": before_entry["value"],
                    },
                    "after": {
                        "state": after_entry["state"],
                        "value": after_entry["value"],
                    },
                }
            )

        for key in changed_keys:
            before_entry = before_entries[key]
            after_entry = after_entries[key]

            state_changed = before_entry["state"] != after_entry["state"]
            value_changed = before_entry["value"] != after_entry["value"]
            kind = "state+value" if (state_changed and value_changed) else (
                "state" if state_changed else "value"
            )

            review_items.append(
                {
                    "id": _review_item_id(int(after.get("character_id") or 0), key),
                    "character_id": int(after.get("character_id") or 0),
                    "character_name": str(after.get("name") or ""),
                    "key": key,
                    "sheet_name": str(after_entry.get("sheet_name") or before_entry.get("sheet_name") or ""),
                    "row_index": int(after_entry.get("row_index") or before_entry.get("row_index") or 0),
                    "row_type": str(after_entry.get("row_type") or before_entry.get("row_type") or "checkbox"),
                    "section_label": str(
                        after_entry.get("section_label") or before_entry.get("section_label") or ""
                    ),
                    "label": str(after_entry.get("label") or before_entry.get("label") or ""),
                    "change_kind": kind,
                    "before": {
                        "state": before_entry["state"],
                        "value": before_entry["value"],
                    },
                    "after": {
                        "state": after_entry["state"],
                        "value": after_entry["value"],
                    },
                    "resolution": _blank_resolution(),
                }
            )

        delta = {
            "done": int(after_counts.get("done") or 0) - int(before_counts.get("done") or 0),
            "todo": int(after_counts.get("todo") or 0) - int(before_counts.get("todo") or 0),
            "excluded": int(after_counts.get("excluded") or 0)
            - int(before_counts.get("excluded") or 0),
            "total": int(after_counts.get("total") or 0) - int(before_counts.get("total") or 0),
        }

        changed = bool(
            added_keys
            or removed_keys
            or changed_keys
            or any(int(v) != 0 for v in delta.values())
            or str(before.get("fingerprint") or "") != str(after.get("fingerprint") or "")
        )
        if changed:
            summary["characters_changed"] += 1

        character_rows.append(
            {
                "name": str(after.get("name") or before.get("name") or ""),
                "character_id": int(after.get("character_id") or before.get("character_id") or 0),
                "status": "present",
                "changed": changed,
                "before": {
                    "counts": before_counts,
                    "active_entries": int(before.get("active_entries") or 0),
                    "fingerprint": str(before.get("fingerprint") or ""),
                },
                "after": {
                    "counts": after_counts,
                    "active_entries": int(after.get("active_entries") or 0),
                    "fingerprint": str(after.get("fingerprint") or ""),
                },
                "delta": delta,
                "entries": {
                    "added_count": len(added_keys),
                    "removed_count": len(removed_keys),
                    "changed_count": len(changed_keys),
                    "added_sample": _sample_entries(added_keys, after_entries, limit=sample_limit),
                    "removed_sample": _sample_entries(removed_keys, before_entries, limit=sample_limit),
                    "changed_sample": changed_sample,
                },
            }
        )

    return summary, character_rows, review_items, advanced_items


def _next_report_path() -> Path:
    root = _report_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / f"between_run_{stamp}.json"
    suffix = 1
    while candidate.exists():
        candidate = root / f"between_run_{stamp}_{suffix:02d}.json"
        suffix += 1
    return candidate


def _path_mtime_sort_key(path: Path) -> tuple[int, str]:
    try:
        return path.stat().st_mtime_ns, path.name.casefold()
    except OSError:
        return 0, path.name.casefold()


def _prune_between_run_reports(*, keep: int = MAX_PERSISTED_BETWEEN_RUN_REPORTS) -> None:
    if keep < 1:
        return
    root = _report_root()
    if not root.exists() or not root.is_dir():
        return
    try:
        files = [path for path in root.glob("between_run_*.json") if path.is_file()]
    except OSError:
        return
    files.sort(key=_path_mtime_sort_key, reverse=True)
    for stale_path in files[keep:]:
        try:
            stale_path.unlink()
        except OSError:
            continue


def create_between_run_report(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    reason: str,
    run_token: tuple[Any, ...] | None = None,
    sample_limit: int = SAMPLE_LIMIT_DEFAULT,
    persist: bool = True,
    baseline: dict[str, Any] | None = None,
    orphaned_by_character: dict[str, int] | None = None,
) -> tuple[dict[str, Any], Path | None]:
    baseline_snapshot = baseline if baseline is not None else load_baseline_snapshot()
    current_snapshot = build_snapshot(
        conn,
        run_id,
        source="current",
        run_token=run_token,
    )

    if baseline_snapshot is None:
        summary = {
            "baseline_available": False,
            "message": (
                "No baseline snapshot found yet. A baseline is saved on project close "
                "and after ingest/import transitions."
            ),
            "characters_compared": 0,
            "characters_changed": 0,
            "characters_added": 0,
            "characters_removed": 0,
            "entries_added": 0,
            "entries_removed": 0,
            "entries_changed": 0,
            "review_unresolved": 0,
            "review_resolved_done": 0,
            "review_resolved_excluded": 0,
            "review_total": 0,
        }
        character_rows: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        advanced_items: list[dict[str, Any]] = []
    else:
        summary, character_rows, review_items, advanced_items = compare_snapshots(
            baseline_snapshot,
            current_snapshot,
            sample_limit=sample_limit,
        )
        review_summary = _summarize_review_items(review_items)
        summary["review_unresolved"] = review_summary["unresolved"]
        summary["review_resolved_done"] = review_summary["accepted"]
        summary["review_resolved_excluded"] = review_summary["reverted"]
        summary["review_total"] = review_summary["total"]

    report_doc: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "reason": reason,
        "baseline": {
            "available": baseline_snapshot is not None,
            "path": str(baseline_path()),
            "captured_at": (
                str(baseline_snapshot.get("captured_at") or "")
                if isinstance(baseline_snapshot, dict)
                else ""
            ),
            "source": (
                str(baseline_snapshot.get("source") or "")
                if isinstance(baseline_snapshot, dict)
                else ""
            ),
            "run": (
                baseline_snapshot.get("run")
                if isinstance(baseline_snapshot, dict)
                else None
            ),
        },
        "current": {
            "captured_at": current_snapshot.get("captured_at"),
            "source": current_snapshot.get("source"),
            "run": current_snapshot.get("run"),
            "totals": current_snapshot.get("totals"),
            "character_count": len(current_snapshot.get("characters") or []),
        },
        "summary": summary,
        "characters": character_rows,
        "review_items": review_items,
        "advanced_items": advanced_items,
        "advanced": {
            "orphaned_by_character": orphaned_by_character or {},
        },
    }

    if not persist:
        return report_doc, None

    out_path = _next_report_path()
    report_doc["report_path"] = str(out_path)
    _write_json(out_path, report_doc)
    _write_json(latest_report_path(), report_doc)
    _prune_between_run_reports()
    return report_doc, out_path
