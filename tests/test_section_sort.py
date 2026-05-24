"""Full coverage for app/section_sort.py — pure classification + ordering."""
from __future__ import annotations

from app import section_sort as ss


# --- mode + track helpers ---------------------------------------------------

def test_normalize_sort_mode():
    assert ss.normalize_sort_mode("workbook") == ss.SORT_MODE_WORKBOOK
    assert ss.normalize_sort_mode("ENDGAME") == ss.SORT_MODE_ENDGAME
    assert ss.normalize_sort_mode(None) == ss.DEFAULT_SORT_MODE
    assert ss.normalize_sort_mode("nonsense") == ss.DEFAULT_SORT_MODE


def test_sort_mode_label():
    assert ss.sort_mode_label("workbook") == "Workbook order"
    assert ss.sort_mode_label("bogus") == ss.SORT_MODE_LABELS[ss.DEFAULT_SORT_MODE]


def test_default_track_and_supports_sheet():
    assert ss.default_track("Miner Logs") == "mining"
    assert ss.default_track("Carpentry Log") == "recipes"
    assert ss.default_track("Folklore Gathering Books") == "gathering"
    assert ss.default_track("Story Quests") is None

    assert ss.supports_sheet("Botanist Logs") is True
    assert ss.supports_sheet("Folklore Gathering Books") is True
    assert ss.supports_sheet("Story Quests") is False        # no track
    assert ss.supports_sheet("Random Sheet") is False        # no "log" token


# --- classify_section: every bucket branch ----------------------------------

def _classify(label, *, sheet="Carpentry Log", scope=None, track=None, row=1):
    state = ss.SectionSortState(track=track or ss.default_track(sheet), scope=scope)
    return ss.classify_section(sheet, label, row, state), state


def test_classify_explicit_tracks():
    for label, track in [
        ("QUARRYING", "quarrying"),
        ("HARVESTING", "harvesting"),
        ("LOGGING", "logging"),
        ("MINING", "mining"),
    ]:
        meta, _ = _classify(label, sheet="Miner Logs")
        assert meta["track"] == track


def test_classify_level_and_special_headers():
    meta, state = _classify("LEVEL BASED RECIPES")
    assert meta["bucket"] == "level_header" and state.scope == "level"

    meta, state = _classify("SPECIAL RECIPES")
    assert meta["bucket"] == "special_header" and state.scope == "special"


def test_classify_collectables_variants():
    meta, _ = _classify("COLLECTABLES", scope="special")
    assert meta["bucket"] == "special_collectables"
    meta, _ = _classify("COLLECTABLES", scope=None)
    assert meta["bucket"] == "collectables"


def test_classify_level_range_variants():
    meta, _ = _classify("LEVELS 1-10")
    assert meta["bucket"] == "level_range"
    assert meta["level_start"] == 1 and meta["level_end"] == 10

    meta, _ = _classify("LEVELS 50-60", scope="special")
    assert meta["bucket"] == "special_level"


def test_classify_folklore():
    meta, state = _classify("REGIONAL FOLKLORE")
    assert meta["bucket"] == "folklore_header" and state.scope == "folklore"
    meta, _ = _classify("Some Folklore Book", scope="folklore")
    assert meta["bucket"] == "folklore_entry"


def test_classify_restoration():
    meta, state = _classify("ISHGARD RESTORATION")
    assert meta["bucket"] == "restoration_header" and state.scope == "restoration"
    meta, _ = _classify("FIRST RESTORATION PHASE")
    assert meta["bucket"] == "restoration_phase" and meta["restoration_phase"] == 1
    meta, _ = _classify("FOURTH RESTORATION PHASE")
    assert meta["restoration_phase"] == 4


def test_classify_master_recipes():
    meta, state = _classify("MASTER RECIPES")
    assert meta["bucket"] == "master_header" and state.scope == "master"
    meta, _ = _classify("MASTER RECIPES (3)")
    assert meta["bucket"] == "master_tier" and meta["master_tier"] == 3
    meta, _ = _classify("OTHER MASTER RECIPES")
    assert meta["bucket"] == "master_other"


def test_classify_misc_buckets():
    # Labels must avoid the earlier LEVEL/SPECIAL prefix branches.
    assert _classify("GRAND COMPANY DELIVERIES")[0]["bucket"] == "deliveries"
    assert _classify("GATHERER TOOLS")[0]["bucket"] == "tools"
    assert _classify("SIDE QUESTS")[0]["bucket"] == "quests"
    assert _classify("OTHER")[0]["bucket"] == "other_header"
    assert _classify("OTHERS")[0]["bucket"] == "other_header"


def test_classify_special_group_fallthrough():
    # An unrecognized label while scope is "special" -> special_group.
    meta, _ = _classify("Mystery Block", scope="special")
    assert meta["bucket"] == "special_group"


def test_classify_default_other():
    meta, _ = _classify("Totally Unmatched Label", sheet="Carpentry Log")
    assert meta["bucket"] == "other" and meta["bucket_rank"] == 80


def test_classify_track_change_resets_scope():
    state = ss.SectionSortState(track="mining", scope="special")
    ss.classify_section("Miner Logs", "QUARRYING", 5, state)
    assert state.scope is None  # explicit track change clears scope


# --- sort_group_dicts -------------------------------------------------------

def _groups(labels):
    return [
        {"section": label, "row_index": i + 2, "section_sort": None}
        for i, label in enumerate(labels)
    ]


def test_sort_workbook_mode_is_identity():
    groups = _groups(["LEVELS 50-60", "LEVELS 1-10"])
    out = ss.sort_group_dicts("Carpentry Log", groups, "workbook")
    assert [g["section"] for g in out] == ["LEVELS 50-60", "LEVELS 1-10"]


def test_sort_unsupported_sheet_is_identity():
    groups = _groups(["LEVELS 50-60", "LEVELS 1-10"])
    out = ss.sort_group_dicts("Story Quests", groups, "progression")
    assert [g["section"] for g in out] == ["LEVELS 50-60", "LEVELS 1-10"]


def test_sort_progression_orders_low_to_high():
    groups = _groups(["LEVEL BASED RECIPES", "LEVELS 50-60", "LEVELS 1-10"])
    out = ss.sort_group_dicts("Carpentry Log", groups, "progression")
    levels = [g["section"] for g in out if g["section"].startswith("LEVELS")]
    assert levels == ["LEVELS 1-10", "LEVELS 50-60"]


def test_sort_endgame_orders_high_to_low():
    groups = _groups(["LEVEL BASED RECIPES", "LEVELS 1-10", "LEVELS 50-60"])
    out = ss.sort_group_dicts("Carpentry Log", groups, "endgame")
    levels = [g["section"] for g in out if g["section"].startswith("LEVELS")]
    assert levels == ["LEVELS 50-60", "LEVELS 1-10"]


def test_sort_leading_non_section_row_stays_first():
    groups = [
        {"section": None, "row_index": 0, "section_sort": None},
        {"section": "LEVELS 10-20", "row_index": 3, "section_sort": None},
        {"section": "LEVELS 1-10", "row_index": 5, "section_sort": None},
    ]
    out = ss.sort_group_dicts("Carpentry Log", groups, "progression")
    assert out[0]["section"] is None


def test_sort_keeps_existing_meta_without_recomputing():
    # A group that already carries section_sort metadata is trusted as-is.
    meta = {"track": "recipes", "bucket": "master_tier", "bucket_rank": 61, "master_tier": 2}
    groups = [
        {"section": "MASTER RECIPES (5)", "row_index": 2, "section_sort": dict(meta)},
        {"section": "MASTER RECIPES (1)", "row_index": 3, "section_sort": None},
    ]
    out = ss.sort_group_dicts("Carpentry Log", groups, "progression")
    # first group kept its injected master_tier=2 meta
    assert out and any(g["section_sort"].get("master_tier") == 2 for g in out)


def test_sort_handles_malformed_meta_values():
    # Exercise the defensive int-parsing branches in _key / _directional_value.
    groups = [
        {"section": "A", "row_index": 2, "section_sort": {"track": "recipes", "bucket_rank": None}},
        {"section": "B", "row_index": 3, "section_sort": {"track": "recipes", "bucket_rank": True}},
        {"section": "C", "row_index": 4, "section_sort": {"track": "recipes", "bucket_rank": "xx"}},
        {"section": "F", "row_index": 7, "section_sort": {"track": "recipes", "bucket_rank": [1]}},
        {"section": "D", "row_index": 5,
         "section_sort": {"track": "recipes", "bucket": "level_range", "level_start": "bad"}},
        {"section": "E", "row_index": 6,
         "section_sort": {"track": "recipes", "bucket": "level_range", "level_start": None}},
        # _directional_value branches: restoration_phase (int), bool, and a
        # non-numeric type that falls through to value=0.
        {"section": "G", "row_index": 8,
         "section_sort": {"track": "recipes", "bucket": "restoration_phase", "restoration_phase": 2}},
        {"section": "H", "row_index": 9,
         "section_sort": {"track": "recipes", "bucket": "level_range", "level_start": True}},
        {"section": "I", "row_index": 10,
         "section_sort": {"track": "recipes", "bucket": "master_tier", "master_tier": [1]}},
    ]
    out = ss.sort_group_dicts("Carpentry Log", groups, "progression")
    assert {g["section"] for g in out} == set("ABCDEFGHI")
