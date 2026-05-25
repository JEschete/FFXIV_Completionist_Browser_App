#!/usr/bin/env python3
"""Report desktop import label collisions and bucket crossings.

This script replays the desktop completion candidate matching logic used by
`app.lodestone_import.import_desktop_completion`, but performs no writes.

A collision is reported when one source candidate resolves to more than one
workbook row after bucket filtering.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from app import db
from app import lodestone_import as li


@dataclass
class MatchContext:
    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]]
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]]
    norm_keys_idx: dict[str, list[str]]
    global_exact_idx: dict[str, list[tuple[str, int, str]]]
    global_norm_idx: dict[str, list[tuple[str, int, str]]]
    global_exact_by_section: dict[str | None, dict[str, list[tuple[str, int, str]]]]
    global_norm_by_section: dict[str | None, dict[str, list[tuple[str, int, str]]]]
    shared_fate_idx: dict[tuple[str, int], list[tuple[str, int, str]]]
    aether_current_idx: dict[tuple[str, int], list[tuple[str, int, str]]]
    blue_mage_idx: dict[tuple[str, str], list[tuple[str, int, str]]]
    classes_jobs_idx: dict[str, list[tuple[str, int, str]]]
    classes_jobs_label_idx: dict[str, list[tuple[str, int, str]]]
    desynthesis_idx: dict[str, list[tuple[str, int, str]]]
    hunting_idx: dict[tuple[str, int], list[tuple[str, int, str]]]
    positional_sheet_idx: dict[str, dict[str, list[tuple[str, int, str]]]]
    society_idx: dict[tuple[str, str], list[tuple[str, int, str]]]
    adventure_plate_row_sections: dict[tuple[str, int, str], str]
    row_context: dict[tuple[str, int, str], dict[str, Any]]


def _collect_desktop_candidates(
    payload: dict[str, Any],
    source_index: dict[str, dict[str, tuple[str, ...]]],
) -> list[dict[str, Any]]:
    """Build candidates from completion payload using importer rules."""
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}

    for path_parts, raw_value in li._walk_leaves(payload.get("overall", {}), ("overall",)):
        if not path_parts:
            continue

        leaf_id = li._normalize_numeric_id(path_parts[-1])
        if leaf_id is None:
            continue

        bucket = li._completion_bucket_from_path(path_parts)
        if bucket is None:
            continue

        state_info = li._decode_completion_value(raw_value)
        if state_info is None:
            continue

        state, pct = state_info
        labels, source_bucket = li._lookup_source_labels(
            source_index,
            bucket=bucket,
            source_id=leaf_id,
        )

        if not labels and li._bucket_tail(bucket) not in li._POSITIONAL_VALUE_BUCKETS:
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

        merged_state, merged_value = li._merge_source_state(
            str(existing.get("source_state") or "excluded"),
            existing.get("value") if isinstance(existing.get("value"), (int, float)) else None,
            state,
            pct,
        )
        existing["source_state"] = merged_state
        existing["value"] = merged_value

        label_pool = {
            str(label).strip()
            for label in existing.get("labels", [])
            if isinstance(label, str)
        }
        label_pool.update(
            str(label).strip()
            for label in (labels or ())
            if isinstance(label, str)
        )
        existing["labels"] = sorted(label for label in label_pool if label)

        if not existing.get("source_bucket") and source_bucket:
            existing["source_bucket"] = source_bucket
        if not existing.get("source_path_parts"):
            existing["source_path_parts"] = [str(part) for part in path_parts]

    return list(aggregated.values())


def _build_match_context(
    *,
    rows: list[sqlite3.Row],
    sheet_section_map: dict[str, str | None],
    resource_root: Path,
) -> MatchContext:
    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    global_exact_idx: dict[str, list[tuple[str, int, str]]] = {}
    global_norm_idx: dict[str, list[tuple[str, int, str]]] = {}
    global_exact_by_section: dict[str | None, dict[str, list[tuple[str, int, str]]]] = {}
    global_norm_by_section: dict[str | None, dict[str, list[tuple[str, int, str]]]] = {}
    shared_fate_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    aether_current_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    blue_mage_rows: list[tuple[str, int, str]] = []
    classes_jobs_rows: list[tuple[str, int, str]] = []
    classes_jobs_label_idx: dict[str, list[tuple[str, int, str]]] = {}
    desynthesis_rows: list[tuple[str, int, str]] = []
    hunting_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    positional_sheet_rows: dict[str, list[tuple[str, int, str]]] = {
        sheet: [] for sheet in li._POSITIONAL_SHEET_BUCKETS.values()
    }
    society_idx: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    adventure_plate_row_sections: dict[tuple[str, int, str], str] = {}
    row_context: dict[tuple[str, int, str], dict[str, Any]] = {}

    for row in rows:
        sheet_name = str(row["sheet_name"])
        label = str(row["label"] or "").strip()
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

        entry = (sheet_name, int(row["row_index"]), str(row["row_type"]))
        section_label = str(row["section_label"] or "")

        row_context[entry] = {
            "sheet_name": sheet_name,
            "section_label": section_label,
            "label": label,
            "row_json_obj": row_json_obj,
        }

        if sheet_name == "Adventurer Plate":
            section_key = li._adventure_plate_section_key(section_label)
            if section_key:
                adventure_plate_row_sections[entry] = section_key

        if sheet_name == "Shared FATE":
            section_key = li._norm_lookup_key(section_label)
            rank_value: int | None = None
            rank_from_label = li._normalize_numeric_id(label)
            if rank_from_label is not None:
                rank_value = int(rank_from_label)
            elif isinstance(row_json_obj, dict):
                rank_from_json = li._normalize_numeric_id(row_json_obj.get("rank"))
                if rank_from_json is not None:
                    rank_value = int(rank_from_json)
            if section_key and rank_value is not None:
                shared_fate_idx.setdefault((section_key, rank_value), []).append(entry)

        if sheet_name == "Aether Currents" and isinstance(row_json_obj, dict):
            section_key = li._norm_lookup_key(section_label)
            current_value = li._normalize_numeric_id(row_json_obj.get("col_2"))
            if section_key and current_value is not None:
                aether_current_idx.setdefault((section_key, int(current_value)), []).append(entry)

        if sheet_name == "Blue Mage Log":
            blue_mage_rows.append(entry)

        if sheet_name == "Classes-Jobs" and row["row_type"] == "value":
            section_norm = li._norm_label(section_label)
            if "desynthesis" in section_norm:
                desynthesis_rows.append(entry)
            else:
                classes_jobs_rows.append(entry)
                for alias in li._classes_jobs_label_aliases(label):
                    norm = li._norm_label(alias)
                    if norm:
                        classes_jobs_label_idx.setdefault(norm, []).append(entry)

        if sheet_name == "Hunting Logs":
            hunting_key = li._parse_hunting_workbook_label(label)
            if hunting_key:
                hunting_idx.setdefault(hunting_key, []).append(entry)

        if sheet_name in positional_sheet_rows:
            positional_sheet_rows[sheet_name].append(entry)

        if sheet_name == "Society Relations":
            society_key = (li._norm_lookup_key(section_label), li._norm_lookup_key(label))
            if all(society_key):
                society_idx.setdefault(society_key, []).append(entry)

        row_section = sheet_section_map.get(sheet_name)
        section_exact = global_exact_by_section.setdefault(row_section, {})
        section_norm = global_norm_by_section.setdefault(row_section, {})
        for idx_label in li._index_labels_for_global(
            node_label=label,
            row_json_obj=row_json_obj,
        ):
            global_exact_idx.setdefault(idx_label.casefold(), []).append(entry)
            section_exact.setdefault(idx_label.casefold(), []).append(entry)
            norm = li._norm_label(idx_label)
            if norm:
                global_norm_idx.setdefault(norm, []).append(entry)
                section_norm.setdefault(norm, []).append(entry)

        for bucket in li._row_buckets_for_sheet(
            sheet_name,
            section_label,
            row_json_obj=row_json_obj,
        ):
            for idx_label in li._index_labels_for_bucket(
                bucket=bucket,
                node_label=label,
                row_json_obj=row_json_obj,
            ):
                exact_idx.setdefault(bucket, {}).setdefault(idx_label.casefold(), []).append(entry)
                norm = li._norm_label(idx_label)
                if norm:
                    norm_idx.setdefault(bucket, {}).setdefault(norm, []).append(entry)

    norm_keys_idx: dict[str, list[str]] = {
        bucket: list(bucket_norm.keys())
        for bucket, bucket_norm in norm_idx.items()
    }

    blue_mage_rows.sort(key=lambda item: item[1])
    classes_jobs_rows.sort(key=lambda item: item[1])
    desynthesis_rows.sort(key=lambda item: item[1])

    blue_mage_idx = li._build_blue_mage_log_position_index(resource_root, blue_mage_rows)
    classes_jobs_idx: dict[str, list[tuple[str, int, str]]] = {
        str(pos): [entry] for pos, entry in enumerate(classes_jobs_rows)
    }
    desynthesis_idx: dict[str, list[tuple[str, int, str]]] = {
        str(pos): [entry] for pos, entry in enumerate(desynthesis_rows)
    }

    positional_sheet_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    for bucket_tail_key, sheet in li._POSITIONAL_SHEET_BUCKETS.items():
        ordered = sorted(positional_sheet_rows.get(sheet, []), key=lambda item: item[1])
        positional_sheet_idx[bucket_tail_key] = {
            str(pos): [entry] for pos, entry in enumerate(ordered)
        }

    return MatchContext(
        exact_idx=exact_idx,
        norm_idx=norm_idx,
        norm_keys_idx=norm_keys_idx,
        global_exact_idx=global_exact_idx,
        global_norm_idx=global_norm_idx,
        global_exact_by_section=global_exact_by_section,
        global_norm_by_section=global_norm_by_section,
        shared_fate_idx=shared_fate_idx,
        aether_current_idx=aether_current_idx,
        blue_mage_idx=blue_mage_idx,
        classes_jobs_idx=classes_jobs_idx,
        classes_jobs_label_idx=classes_jobs_label_idx,
        desynthesis_idx=desynthesis_idx,
        hunting_idx=hunting_idx,
        positional_sheet_idx=positional_sheet_idx,
        society_idx=society_idx,
        adventure_plate_row_sections=adventure_plate_row_sections,
        row_context=row_context,
    )


def _resolve_hits_for_candidate(
    candidate: dict[str, Any],
    *,
    ctx: MatchContext,
    starting_class: str | None,
) -> tuple[list[tuple[str, int, str]], list[str]]:
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
    match_labels = li._candidate_match_labels(labels)

    bucket_tail = li._bucket_tail(bucket)
    if not match_labels and bucket_tail not in li._POSITIONAL_VALUE_BUCKETS:
        return [], []

    hits: list[tuple[str, int, str]] | None = None

    if bucket_tail == "classes-jobs":
        for source_label in match_labels:
            for alias in li._classes_jobs_label_aliases(source_label):
                norm = li._norm_label(alias)
                if not norm:
                    continue
                label_hits = li._dedupe_hits(ctx.classes_jobs_label_idx.get(norm))
                if label_hits and len(label_hits) == 1:
                    hits = label_hits
                    break
            if hits:
                break
        if not hits:
            hits = ctx.classes_jobs_idx.get(source_id)
    elif bucket_tail == "desynthesis":
        hits = ctx.desynthesis_idx.get(source_id)
    elif bucket_tail in ctx.positional_sheet_idx:
        hits = ctx.positional_sheet_idx[bucket_tail].get(source_id)
    elif bucket_tail == "societal-relations":
        for source_label in match_labels:
            parsed = li._parse_society_rank(source_label)
            if not parsed:
                continue
            hits = ctx.society_idx.get(parsed)
            if hits:
                break

    if bucket.startswith("character/blue-mage/log/"):
        hits = ctx.blue_mage_idx.get((bucket, source_id))

    if not hits and bucket.startswith("logs/hunting/"):
        for source_label in match_labels:
            parsed = li._parse_hunting_source_label(source_label)
            if not parsed:
                continue
            hits = ctx.hunting_idx.get(parsed)
            if hits:
                break

    if not hits and bucket.startswith("travel/shared-fate/"):
        for source_label in match_labels:
            parsed = li._parse_place_rank(source_label)
            if not parsed:
                continue
            zone_key, rank_value = parsed
            hits = ctx.shared_fate_idx.get((zone_key, rank_value))
            if hits:
                break

    if not hits and bucket.startswith("travel/aether-currents/"):
        zone_key = li._aether_zone_from_path(source_path_parts)
        if zone_key:
            for source_label in match_labels:
                current_value = li._parse_current_index(source_label)
                if current_value is None:
                    continue
                hits = ctx.aether_current_idx.get((zone_key, current_value))
                if hits:
                    break

    candidate_section = li._completion_top_section(source_path_parts)
    is_sectionless = candidate_section is None

    aliases: list[str] = []
    if not hits and bucket_tail not in li._EXCLUSIVE_MATCH_BUCKET_TAILS:
        seen_aliases: set[str] = set()
        for source_label in match_labels:
            for base_alias in li._candidate_aliases(bucket, source_label):
                if is_sectionless:
                    ordered_aliases = [base_alias]
                else:
                    expanded = li._generic_label_aliases(base_alias)
                    ordered_aliases = [base_alias] + sorted(
                        alias for alias in expanded if alias != base_alias
                    )
                for alias in ordered_aliases:
                    key = alias.casefold()
                    if key in seen_aliases:
                        continue
                    seen_aliases.add(key)
                    aliases.append(alias)

        bucket_exact: dict[str, list[tuple[str, int, str]]] = {}
        bucket_norm: dict[str, list[tuple[str, int, str]]] = {}
        bucket_norm_keys: list[str] = []
        for bucket_key in li._bucket_lookup_chain(bucket):
            candidate_exact = ctx.exact_idx.get(bucket_key)
            candidate_norm = ctx.norm_idx.get(bucket_key)
            if candidate_exact or candidate_norm:
                bucket_exact = candidate_exact or {}
                bucket_norm = candidate_norm or {}
                bucket_norm_keys = ctx.norm_keys_idx.get(bucket_key, [])
                break

        for alias in aliases:
            hits = bucket_exact.get(alias.casefold())
            if hits:
                break
            norm = li._norm_label(alias)
            hits = bucket_norm.get(norm)
            if hits:
                break

        if not hits and not is_sectionless:
            hits = li._partial_match_hits(
                bucket=bucket,
                aliases=aliases,
                bucket_norm=bucket_norm,
                bucket_norm_keys=bucket_norm_keys,
            )

        if is_sectionless:
            section_global_exact = ctx.global_exact_idx
            section_global_norm = ctx.global_norm_idx
        else:
            section_global_exact = ctx.global_exact_by_section.get(candidate_section, {})
            section_global_norm = ctx.global_norm_by_section.get(candidate_section, {})

        if not hits:
            for alias in aliases:
                hits = section_global_exact.get(alias.casefold())
                if hits:
                    break
                norm = li._norm_label(alias)
                hits = section_global_norm.get(norm)
                if hits:
                    break

        if not hits and not is_sectionless:
            if bucket == "quest":
                quest_aliases = [alias for alias in aliases if " " in li._norm_label(alias)]
                quest_section_norm = {
                    key: val
                    for key, val in section_global_norm.items()
                    if " " in key
                }
                hits = li._partial_match_hits_generic(
                    quest_aliases,
                    quest_section_norm,
                    cutoff=0.95,
                )
            else:
                hits = li._partial_match_hits_generic(
                    aliases,
                    section_global_norm,
                    cutoff=0.92,
                )

        if is_sectionless:
            deduped_sectionless = li._dedupe_hits(hits) or []
            if deduped_sectionless and len(deduped_sectionless) != 1:
                hits = None

    deduped = li._dedupe_hits(li._filter_hits_for_bucket(bucket, hits))
    deduped = li._dedupe_hits(
        li._filter_island_sanctuary_hits(
            bucket=bucket,
            hits=deduped,
            source_state=source_state,
            source_value=source_value,
        )
    )
    deduped = li._dedupe_hits(
        li._filter_adventure_plate_hits_by_sections(
            bucket=bucket,
            source_labels=labels,
            hits=deduped,
            row_sections=ctx.adventure_plate_row_sections,
        )
    )
    deduped = li._dedupe_hits(
        li._filter_fate_hits(
            bucket=bucket,
            hits=deduped,
        )
    )
    deduped = li._dedupe_hits(
        li._filter_gathering_log_hits_by_type(
            bucket=bucket,
            hits=deduped,
            row_context=ctx.row_context,
        )
    )
    deduped = li._dedupe_hits(
        li._filter_crafting_log_hits(
            bucket=bucket,
            match_labels=match_labels,
            hits=deduped,
            row_context=ctx.row_context,
        )
    )
    deduped = li._dedupe_hits(
        li._filter_quest_hits_by_source_tokens(
            bucket=bucket,
            source_labels=labels,
            hits=deduped,
            row_context=ctx.row_context,
            starting_class=starting_class,
        )
    )
    deduped = li._dedupe_hits(
        li._filter_hits_by_unlock_field(
            bucket=bucket,
            match_labels=match_labels,
            hits=deduped,
            row_context=ctx.row_context,
        )
    )
    deduped = li._dedupe_hits(
        li._select_progression_hit(
            bucket=bucket,
            match_labels=match_labels,
            hits=deduped,
        )
    )
    deduped = li._dedupe_hits(
        li._collapse_duplicate_signature_hits(
            bucket=bucket,
            hits=deduped,
            row_context=ctx.row_context,
        )
    ) or []
    return deduped, aliases


def _run_collision_report(
    *,
    completion_path: Path,
    db_path: Path,
    run_id: int | None,
    resource_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    payload = li.load_completion_payload(completion_path)
    effective_starting_class = li._completion_payload_starting_class(payload)

    source_index = li._build_source_label_index(str(resource_root))
    inline_source_index = li._build_inline_completion_source_index(payload)
    if inline_source_index:
        source_index = li._merge_source_indexes(source_index, inline_source_index)

    candidates = _collect_desktop_candidates(payload, source_index)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        effective_run_id = int(run_id if run_id is not None else db.latest_run_id(conn) or 0)
        if effective_run_id <= 0:
            raise ValueError("No ingest run found in database")

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
            (effective_run_id,),
        ).fetchall()

        sheet_section_map = li._build_sheet_section_map(conn, effective_run_id)
        ctx = _build_match_context(
            rows=rows,
            sheet_section_map=sheet_section_map,
            resource_root=resource_root,
        )
    finally:
        conn.close()

    collision_rows: list[dict[str, Any]] = []
    hit_rows: list[dict[str, Any]] = []
    matched_count = 0
    quarantined_candidates = 0

    for candidate in candidates:
        bucket = str(candidate.get("bucket") or "")
        if li._is_quarantined_bucket(bucket):
            quarantined_candidates += 1
            continue

        hits, aliases = _resolve_hits_for_candidate(
            candidate,
            ctx=ctx,
            starting_class=effective_starting_class,
        )
        if hits:
            matched_count += 1
        labels = [str(label) for label in candidate.get("labels", []) if isinstance(label, str)]
        match_labels = li._candidate_match_labels(labels)
        if len(hits) <= 1 or li._allows_multi_hit_candidate(
            bucket=bucket,
            source_labels=labels,
            match_labels=match_labels,
            hits=hits,
            row_context=ctx.row_context,
        ):
            continue

        source_bucket = str(candidate.get("source_bucket") or "")
        source_id = str(candidate.get("source_id") or "")
        source_state = str(candidate.get("source_state") or "")
        primary_label = match_labels[0] if match_labels else f"id:{source_id}"
        source_path = "/".join(str(p) for p in candidate.get("source_path_parts") or ())

        workbook_buckets = sorted({
            sheet_bucket
            for sheet_name, _, _ in hits
            for sheet_bucket in li._sheet_buckets(str(sheet_name))
        })
        crossed_buckets = sorted(
            bucket_name for bucket_name in workbook_buckets if bucket_name != bucket
        )

        hit_sheets = sorted({sheet_name for sheet_name, _, _ in hits})
        collision_flags: list[str] = []
        if len(hit_sheets) > 1:
            collision_flags.append("multi_sheet")
        if crossed_buckets:
            collision_flags.append("cross_bucket")
        if source_bucket and source_bucket != bucket:
            collision_flags.append("source_bucket_fallback")

        row_record = {
            "bucket": bucket,
            "source_bucket": source_bucket,
            "source_id": source_id,
            "source_state": source_state,
            "label": primary_label,
            "labels": labels,
            "source_path": source_path,
            "aliases": aliases,
            "hit_count": len(hits),
            "hit_sheets": hit_sheets,
            "workbook_buckets": workbook_buckets,
            "crossed_buckets": crossed_buckets,
            "collision_flags": collision_flags,
            "hits": [
                {
                    "sheet_name": str(sheet_name),
                    "row_index": int(row_index),
                    "row_type": str(row_type),
                    "sheet_buckets": sorted(li._sheet_buckets(str(sheet_name))),
                }
                for sheet_name, row_index, row_type in hits
            ],
        }
        collision_rows.append(row_record)

        for sheet_name, row_index, row_type in hits:
            hit_rows.append(
                {
                    "bucket": bucket,
                    "source_bucket": source_bucket,
                    "source_id": source_id,
                    "label": primary_label,
                    "source_state": source_state,
                    "source_path": source_path,
                    "sheet_name": str(sheet_name),
                    "row_index": int(row_index),
                    "row_type": str(row_type),
                    "sheet_buckets": "|".join(sorted(li._sheet_buckets(str(sheet_name)))),
                    "crossed_buckets": "|".join(crossed_buckets),
                    "collision_flags": "|".join(collision_flags),
                }
            )

    collision_rows.sort(
        key=lambda item: (
            -int(item["hit_count"]),
            str(item["bucket"]),
            str(item["label"]).casefold(),
            str(item["source_id"]),
        )
    )

    meta = {
        "completion_path": str(completion_path),
        "db_path": str(db_path),
        "run_id": int(effective_run_id),
        "effective_run_id": int(effective_run_id),
        "resource_root": str(resource_root),
        "source_buckets": len(source_index),
        "total_candidates": len(candidates),
        "matched_candidates": matched_count,
        "quarantined_candidates": quarantined_candidates,
        "collision_candidates": len(collision_rows),
        "collision_hits": sum(int(item["hit_count"]) for item in collision_rows),
    }
    return meta, collision_rows, hit_rows


def _write_reports(
    *,
    output_prefix: Path,
    meta: dict[str, Any],
    collisions: list[dict[str, Any]],
    hit_rows: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)

    json_path = output_prefix.with_suffix(".json")
    summary_csv_path = output_prefix.with_suffix(".csv")
    hits_csv_path = output_prefix.parent / f"{output_prefix.name}_hits.csv"

    json_path.write_text(
        json.dumps(
            {
                "meta": meta,
                "collisions": collisions,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    with summary_csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "bucket",
                "source_bucket",
                "source_id",
                "source_state",
                "label",
                "source_path",
                "hit_count",
                "hit_sheets",
                "workbook_buckets",
                "crossed_buckets",
                "collision_flags",
            ],
        )
        writer.writeheader()
        for item in collisions:
            writer.writerow(
                {
                    "bucket": item["bucket"],
                    "source_bucket": item["source_bucket"],
                    "source_id": item["source_id"],
                    "source_state": item["source_state"],
                    "label": item["label"],
                    "source_path": item["source_path"],
                    "hit_count": item["hit_count"],
                    "hit_sheets": " | ".join(item["hit_sheets"]),
                    "workbook_buckets": "|".join(item["workbook_buckets"]),
                    "crossed_buckets": "|".join(item["crossed_buckets"]),
                    "collision_flags": "|".join(item["collision_flags"]),
                }
            )

    with hits_csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "bucket",
                "source_bucket",
                "source_id",
                "label",
                "source_state",
                "source_path",
                "sheet_name",
                "row_index",
                "row_type",
                "sheet_buckets",
                "crossed_buckets",
                "collision_flags",
            ],
        )
        writer.writeheader()
        writer.writerows(hit_rows)

    return json_path, summary_csv_path, hits_csv_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report desktop completion collisions and bucket crossings.",
    )
    parser.add_argument(
        "--completion",
        required=True,
        type=Path,
        help="Path to desktop completion JSON (e.g. completion.json).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(db.DB_PATH),
        help=f"SQLite database path (default: {db.DB_PATH}).",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Ingest run id to analyze (default: latest run in DB).",
    )
    parser.add_argument(
        "--resource-root",
        type=Path,
        default=None,
        help=(
            "Desktop app resource root containing completion_data/; "
            "default is auto-detected via lodestone_import.resolve_resource_root()."
        ),
    )
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=None,
        help="Output path prefix for reports (without extension).",
    )
    parser.add_argument(
        "--max-print",
        type=int,
        default=50,
        help="Maximum number of collision rows to print to stdout.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    completion_path = args.completion.expanduser().resolve()
    db_path = args.db.expanduser().resolve()

    resource_root = args.resource_root
    if resource_root is None:
        resource_root = li.resolve_resource_root()
        if resource_root is None:
            print(
                "ERROR: Could not auto-detect resource root. "
                "Pass --resource-root explicitly.",
                file=sys.stderr,
            )
            return 2
    resource_root = Path(resource_root).expanduser().resolve()

    if not completion_path.exists():
        print(f"ERROR: completion file not found: {completion_path}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"ERROR: db file not found: {db_path}", file=sys.stderr)
        return 2

    if args.output_prefix is None:
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = completion_path.stem or "completion"
        output_prefix = Path("data/logs") / f"desktop_collisions_{stem}_{timestamp}"
    else:
        output_prefix = args.output_prefix.expanduser().resolve()

    try:
        meta, collisions, hit_rows = _run_collision_report(
            completion_path=completion_path,
            db_path=db_path,
            run_id=args.run_id,
            resource_root=resource_root,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    json_path, summary_csv_path, hits_csv_path = _write_reports(
        output_prefix=output_prefix,
        meta=meta,
        collisions=collisions,
        hit_rows=hit_rows,
    )

    print(
        f"Analyzed {meta['total_candidates']} candidates; "
        f"matched {meta['matched_candidates']}; "
        f"collision candidates {meta['collision_candidates']}"
    )
    print(f"Wrote JSON report: {json_path}")
    print(f"Wrote summary CSV: {summary_csv_path}")
    print(f"Wrote hit CSV: {hits_csv_path}")

    if collisions and args.max_print > 0:
        print("\nTop collisions:")
        for item in collisions[: args.max_print]:
            crossed = ",".join(item["crossed_buckets"]) or "-"
            flags = ",".join(item["collision_flags"]) or "-"
            sheets = " | ".join(item["hit_sheets"])
            print(
                f"[{item['bucket']}] {item['label']} (id {item['source_id']}) "
                f"hits={item['hit_count']} crossed={crossed} flags={flags}"
            )
            print(f"  sheets: {sheets}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
