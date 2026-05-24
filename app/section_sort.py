"""Section ordering helpers for crafting and gathering log sheets.

The workbook keeps authoritative row order. This module adds an optional,
predictable section-order layer for log-style sheets by classifying section
labels into coarse buckets (level ranges, special blocks, master recipe tiers,
etc.) while preserving workbook row order as a stable tie-breaker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SORT_MODE_WORKBOOK = "workbook"
SORT_MODE_PROGRESSION = "progression"
SORT_MODE_ENDGAME = "endgame"
SORT_MODE_CHOICES = (
    SORT_MODE_WORKBOOK,
    SORT_MODE_PROGRESSION,
    SORT_MODE_ENDGAME,
)
SORT_MODE_LABELS = {
    SORT_MODE_WORKBOOK: "Workbook order",
    SORT_MODE_PROGRESSION: "Progression (low to high)",
    SORT_MODE_ENDGAME: "Endgame first (high to low)",
}
DEFAULT_SORT_MODE = SORT_MODE_PROGRESSION

_LEVEL_RANGE_RE = re.compile(r"^LEVELS?\s+(\d+)\s*-\s*(\d+)$")
_MASTER_TIER_RE = re.compile(r"^MASTER RECIPES\s*\((\d+)\)$")
_PHASE_RE = re.compile(r"^(FIRST|SECOND|THIRD|FOURTH) RESTORATION PHASE$")

_TRACK_BY_SHEET_TOKEN = (
    ("miner logs", "mining"),
    ("botanist logs", "logging"),
    ("fishing logs", "fishing"),
    ("carpentry log", "recipes"),
    ("blacksmithing log", "recipes"),
    ("armorcrafting log", "recipes"),
    ("goldsmithing log", "recipes"),
    ("leatherworking log", "recipes"),
    ("weaving log", "recipes"),
    ("alchemy log", "recipes"),
    ("culinary log", "recipes"),
    ("shared craft log", "recipes"),
    ("folklore gathering books", "gathering"),
)

_RESTORATION_PHASE_ORDINAL = {
    "FIRST": 1,
    "SECOND": 2,
    "THIRD": 3,
    "FOURTH": 4,
}


@dataclass
class SectionSortState:
    """Per-sheet classification state for track/scope-aware section parsing."""

    track: str | None
    scope: str | None = None


def normalize_sort_mode(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value if value in SORT_MODE_CHOICES else DEFAULT_SORT_MODE


def sort_mode_label(mode: str) -> str:
    return SORT_MODE_LABELS[normalize_sort_mode(mode)]


def default_track(sheet_name: str) -> str | None:
    name = sheet_name.lower()
    for token, track in _TRACK_BY_SHEET_TOKEN:
        if token in name:
            return track
    return None


def supports_sheet(sheet_name: str) -> bool:
    name = sheet_name.lower()
    if "log" not in name and "gathering books" not in name:
        return False
    return default_track(sheet_name) is not None


def _norm_label(label: str | None) -> str:
    text = (label or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip().upper()
    return text


def _infer_explicit_track(label_norm: str) -> str | None:
    # Order matters: quarrying and harvesting are subsets of miner/botanist.
    if "QUARRY" in label_norm:
        return "quarrying"
    if "HARVEST" in label_norm:
        return "harvesting"
    if "LOGGING" in label_norm:
        return "logging"
    if "MINING" in label_norm:
        return "mining"
    if label_norm.startswith("LEVEL BASED RECIPES") or label_norm.startswith("SPECIAL RECIPES"):
        return "recipes"
    return None


def classify_section(
    sheet_name: str,
    section_label: str,
    row_index: int,
    state: SectionSortState,
) -> dict[str, object]:
    """Classify a section banner into a stable sort metadata payload."""
    normalized = _norm_label(section_label)
    explicit_track = _infer_explicit_track(normalized)
    if explicit_track is not None:
        # Track changes indicate a new logical block in sheets like Miner/Botanist.
        if explicit_track != state.track:
            state.scope = None
        state.track = explicit_track

    track = state.track or default_track(sheet_name) or "default"
    bucket = "other"
    bucket_rank = 80
    level_start: int | None = None
    level_end: int | None = None
    master_tier: int | None = None
    restoration_phase: int | None = None

    level_match = _LEVEL_RANGE_RE.match(normalized)
    master_match = _MASTER_TIER_RE.match(normalized)
    phase_match = _PHASE_RE.match(normalized)

    if normalized.startswith("LEVEL BASED "):
        state.scope = "level"
        bucket = "level_header"
        bucket_rank = 10
    elif normalized.startswith("SPECIAL "):
        state.scope = "special"
        bucket = "special_header"
        bucket_rank = 30
    elif normalized == "COLLECTABLES":
        bucket = "special_collectables" if state.scope == "special" else "collectables"
        bucket_rank = 34 if state.scope == "special" else 45
        state.scope = "collectables"
    elif level_match:
        level_start = int(level_match.group(1))
        level_end = int(level_match.group(2))
        if state.scope == "special":
            bucket = "special_level"
            bucket_rank = 36
        else:
            bucket = "level_range"
            bucket_rank = 20
    elif normalized == "REGIONAL FOLKLORE":
        state.scope = "folklore"
        bucket = "folklore_header"
        bucket_rank = 40
    elif state.scope == "folklore":
        bucket = "folklore_entry"
        bucket_rank = 41
    elif normalized in {"RESTORATION", "ISHGARD RESTORATION"}:
        state.scope = "restoration"
        bucket = "restoration_header"
        bucket_rank = 70
    elif phase_match:
        state.scope = "restoration"
        restoration_phase = _RESTORATION_PHASE_ORDINAL.get(phase_match.group(1))
        bucket = "restoration_phase"
        bucket_rank = 71
    elif normalized == "MASTER RECIPES":
        state.scope = "master"
        bucket = "master_header"
        bucket_rank = 60
    elif master_match:
        state.scope = "master"
        master_tier = int(master_match.group(1))
        bucket = "master_tier"
        bucket_rank = 61
    elif normalized == "OTHER MASTER RECIPES":
        state.scope = "master"
        bucket = "master_other"
        bucket_rank = 62
    elif "DELIVERIES" in normalized:
        state.scope = "deliveries"
        bucket = "deliveries"
        bucket_rank = 76
    elif "TOOLS" in normalized:
        state.scope = "tools"
        bucket = "tools"
        bucket_rank = 77
    elif "QUESTS" in normalized:
        state.scope = "quests"
        bucket = "quests"
        bucket_rank = 78
    elif normalized in {"OTHER", "OTHERS"}:
        state.scope = "other"
        bucket = "other_header"
        bucket_rank = 90
    elif state.scope == "special":
        bucket = "special_group"
        bucket_rank = 33

    return {
        "track": track,
        "scope": state.scope or "",
        "bucket": bucket,
        "bucket_rank": bucket_rank,
        "level_start": level_start,
        "level_end": level_end,
        "master_tier": master_tier,
        "restoration_phase": restoration_phase,
        "fallback_row": row_index,
    }


def sort_group_dicts(
    sheet_name: str,
    groups: list[dict],
    mode: str,
) -> list[dict]:
    """Return groups sorted by classified section metadata.

    Expects each group dict to carry:
      - section: str | None
      - row_index: int
      - section_sort: dict | None (optional)

    Missing section_sort payloads are inferred from section labels on the fly,
    so old DB runs keep working even before a fresh ingest.
    """
    normalized_mode = normalize_sort_mode(mode)
    if normalized_mode == SORT_MODE_WORKBOOK or not supports_sheet(sheet_name):
        return groups

    state = SectionSortState(track=default_track(sheet_name))
    track_order: dict[str, int] = {}

    for group in groups:
        section = group.get("section")
        if not section:
            continue

        row_index = int(group.get("row_index") or 0)
        existing_meta = group.get("section_sort")
        if isinstance(existing_meta, dict):
            # Keep state transitions deterministic even when metadata is present.
            classify_section(sheet_name, str(section), row_index, state)
            meta = existing_meta
        else:
            meta = classify_section(sheet_name, str(section), row_index, state)
            group["section_sort"] = meta

        track = str(meta.get("track") or "default")
        if track not in track_order:
            track_order[track] = len(track_order)

    def _directional_value(meta: dict[str, object]) -> int:
        bucket = str(meta.get("bucket") or "")
        if bucket in {"level_range", "special_level"}:
            raw = meta.get("level_start")
        elif bucket == "master_tier":
            raw = meta.get("master_tier")
        elif bucket == "restoration_phase":
            raw = meta.get("restoration_phase")
        else:
            return 0

        try:
            if raw is None:
                value = 0
            elif isinstance(raw, bool):
                value = int(raw)
            elif isinstance(raw, (int, float, str)):
                value = int(raw)
            else:
                value = 0
        except (TypeError, ValueError):
            value = 0
        return value if normalized_mode == SORT_MODE_PROGRESSION else -value

    def _key(group: dict) -> tuple[int, int, int, int]:
        section = group.get("section")
        row_index = int(group.get("row_index") or 0)
        if not section:
            # Keep non-section leading rows ahead of sorted section blocks.
            return (-1, 0, 0, row_index)

        raw_meta = group.get("section_sort")
        meta: dict[str, object] = raw_meta if isinstance(raw_meta, dict) else {}
        track = str(meta.get("track") or "default")
        track_rank = track_order.get(track, 999)
        raw_bucket_rank = meta.get("bucket_rank")
        try:
            if raw_bucket_rank is None:
                bucket_rank = 999
            elif isinstance(raw_bucket_rank, bool):
                bucket_rank = int(raw_bucket_rank)
            elif isinstance(raw_bucket_rank, (int, float, str)):
                bucket_rank = int(raw_bucket_rank)
            else:
                bucket_rank = 999
        except (TypeError, ValueError):
            bucket_rank = 999
        return (track_rank, bucket_rank, _directional_value(meta), row_index)

    return sorted(groups, key=_key)
