"""Coverage for app/lodestone_import.py — alias helpers, candidate collection,
and both import engines (Lodestone JSON + desktop completion) end-to-end
against the synthetic fixture DB.
"""
from __future__ import annotations

import json

import pytest

from app import lodestone_import as li


# --- normalization + alias helpers -----------------------------------------

def test_norm_label():
    assert li._norm_label("Vana'diel") == "vanadiel"
    assert li._norm_label("Æther—Test") == "aether test"


def test_strip_marker_prefixes():
    assert li._strip_marker_prefixes("» A Quest") == "A Quest"
    assert li._strip_marker_prefixes("'Quoted'") == "'Quoted'"
    assert li._strip_marker_prefixes("Α Test of Wιll") == "Α Test of Wιll"


def test_qualifier_reorder_aliases():
    # Desktop "<duty> (Savage) - Turn N" should also match the workbook layout
    # "<duty> - Turn N (Savage)" and vice versa.
    fwd = li._qualifier_reorder_aliases("The Second Coil of Bahamut (Savage) - Turn 1")
    assert "The Second Coil of Bahamut - Turn 1 (Savage)" in fwd

    rev = li._qualifier_reorder_aliases("The Second Coil of Bahamut - Turn 1 (Savage)")
    assert "The Second Coil of Bahamut (Savage) - Turn 1" in rev

    # Routed through the duty raid-finder bucket aliases.
    raid_aliases = li._candidate_aliases(
        "duty/duty-raid-finder/raid",
        "The Second Coil of Bahamut (Savage) - Turn 4",
    )
    assert "The Second Coil of Bahamut - Turn 4 (Savage)" in raid_aliases

    # Labels without the qualifier/turn pattern are left untouched.
    assert li._qualifier_reorder_aliases("Eden's Verse: Furor (Savage)") == {
        "Eden's Verse: Furor (Savage)"
    }


def test_quest_label_aliases():
    aliases = li._quest_label_aliases("Abalathian Sidequests (A Cropper's Duty)")
    assert "A Cropper's Duty" in aliases
    assert any("Abalathian" in a for a in aliases)

    renamed = li._quest_label_aliases("Hither and Yarns")
    assert "Hither and Yams" in renamed

    renamed2 = li._quest_label_aliases("Crossing Paths")
    assert "Crossroads" in renamed2


def test_add_candidate_filters():
    pool: dict[str, set] = {}
    li._add_candidate(pool, "minion", "  Wind-up Cursor ")
    li._add_candidate(pool, "minion", 123)        # non-str ignored
    li._add_candidate(pool, "tripletriad", "???")  # placeholder ignored
    assert pool["minion"] == {"Wind-up Cursor"}
    assert "tripletriad" not in pool


def test_collect_candidates():
    payload = {
        "minions": {"items": [{"name": "Wind-up Cursor"}]},
        "mounts": {"items": [{"name": "Company Chocobo"}]},
        "achievements": {"entries": [{"title": "First Steps"}]},
        "authenticated_pages": {
            "quest": {"entries": [{"title": "Quest Alpha"}]},
            "goldsaucer/tripletriad": {"entries": [{"name": "Dodo"}]},
            "bluemage": {"entries": [{"name": "Water Cannon"}]},
            "emote": {"entries": [{"name": "/wave"}]},
            "orchestrion": {"entries": [{"name": "Song A"}]},
        },
    }
    cands = li.collect_candidates(payload)
    assert cands["minion"] == {"Wind-up Cursor"}
    assert cands["quest"] == {"Quest Alpha"}
    assert cands["tripletriad"] == {"Dodo"}
    assert cands["bluemagic"] == {"Water Cannon"}


def test_sheet_buckets():
    assert "quest" in li._sheet_buckets("Story Quests")
    assert li._sheet_buckets("Fishing Leves") == {"quest"}
    assert li._sheet_buckets("Carpentry Leves") == {"quest"}
    assert "minion" in li._sheet_buckets("Minion Guide")
    assert li._sheet_buckets("Triple Triad Cards") == {"tripletriad"}
    assert li._sheet_buckets("Quests Achievements") == {"achievement"}
    assert li._sheet_buckets("Goldsmithing Log") == {"logs/crafting-log/goldsmith"}
    assert li._sheet_buckets("Mount Speed") == {"travel/mount-speed"}
    assert li._sheet_buckets("Bozja - Duties") == {"duty/exploratory-missions/bozja/duties"}
    assert li._sheet_buckets("Miner Logs") == {
        "logs/gathering/gathering-log/mining",
        "logs/gathering/gathering-log/quarrying",
    }
    assert li._sheet_buckets("Triple Triad Opponents") == {
        "character/gold-saucer/triple-triad-opponents"
    }
    assert li._sheet_buckets("Hall of the Novice") == {"duty/hall-of-the-novice"}
    assert li._sheet_buckets("Random Sheet") == set()


def test_alias_generators():
    assert "Painted Glory" in li._orchestrion_label_aliases("Painted Glory Orchestrion Roll")
    assert "Painted Glory Orchestrion Roll" in li._orchestrion_label_aliases("Painted Glory")

    triad = li._tripletriad_label_aliases("Bahamut")
    assert "Bahamut Card" in triad and "The Bahamut" in triad

    assert "Heavensward 2" in li._suffix_roman_digit_aliases("Heavensward II")
    assert "Heavensward II" in li._suffix_roman_digit_aliases("Heavensward 2")

    ach = li._achievement_label_aliases("To Crush Your Enemies II")
    assert "To Crush Your Enemies 2" in ach
    assert "And so on…" in li._achievement_label_aliases("And so on...")


def test_candidate_aliases_dispatch():
    assert any("Card" in a for a in li._candidate_aliases("tripletriad", "Dodo"))
    assert li._candidate_aliases("minion", "Wind-up Cursor") == ["Wind-up Cursor"]
    lucis = li._candidate_aliases("character/relic-gear/lucis-tools", "Halcyon")
    assert "Halcyon Rod" in lucis
    porters = li._candidate_aliases("travel/porters/the-black-shroud", "The Hawthorne Hut")
    assert "Hawthorne Hut" in porters
    sightseeing = li._candidate_aliases("logs/sightseeing-log/a-realm-reborn", "The Brewer's Beacon")
    assert "Brewer's Beacon" in sightseeing


def test_index_labels_for_bucket_splits_quest_pairs():
    labels = li._index_labels_for_bucket(
        bucket="quest", node_label="Combined",
        row_json_obj={"quest": "Training with Leih / School of Hard Nocks"},
    )
    assert "Training with Leih" in labels and "School of Hard Nocks" in labels


def test_index_labels_for_global():
    labels = li._index_labels_for_global(
        node_label="Main", row_json_obj={"a": "Extra", "b": ["List One", "List Two"], "c": 5}
    )
    assert {"Main", "Extra", "List One", "List Two"} <= labels


def test_generic_label_aliases():
    out = li._generic_label_aliases("@SOCIETY.ARKASODARA - REPUTATION RANK 3")
    assert any("Arkasodara" in a for a in out)
    duty = li._generic_label_aliases("D.42")
    assert "Duty 42" in duty and "42" in duty


# --- completion decode / merge helpers --------------------------------------

def test_decode_completion_value():
    assert li._decode_completion_value("Y") == ("done", None)
    assert li._decode_completion_value("X") == ("excluded", None)
    assert li._decode_completion_value("75") == ("value", 75.0)
    assert li._decode_completion_value("324.52") == ("value", 324.52)
    assert li._decode_completion_value(50) == ("value", 50.0)
    assert li._decode_completion_value(True) is None
    assert li._decode_completion_value("nope") is None


def test_walk_leaves():
    leaves = dict(li._walk_leaves({"a": {"b": 1, "c": "Y"}}))
    assert leaves[("a", "b")] == 1
    assert leaves[("a", "c")] == "Y"


def test_merge_source_state_priority():
    # done beats value beats excluded; equal value takes the max.
    assert li._merge_source_state("excluded", None, "done", None) == ("done", None)
    assert li._merge_source_state("done", None, "value", 10.0) == ("done", None)
    assert li._merge_source_state("value", 5.0, "value", 9.0) == ("value", 9.0)
    assert li._merge_source_state("done", None, "done", None) == ("done", None)


def test_merge_row_action():
    actions: dict = {}
    li._merge_row_action(actions, ("S", 1), row_type="checkbox", state="excluded", value=None)
    li._merge_row_action(actions, ("S", 1), row_type="checkbox", state="done", value=None)
    assert actions[("S", 1)]["state"] == "done"


def test_normalize_numeric_id():
    assert li._normalize_numeric_id("x42") == "42"
    assert li._normalize_numeric_id(7) == "7"
    assert li._normalize_numeric_id(7.0) == "7"
    assert li._normalize_numeric_id(7.5) is None
    assert li._normalize_numeric_id(True) is None
    assert li._normalize_numeric_id("abc") is None


def test_path_group_key_and_bucket_from_path():
    assert li._completion_bucket_from_path(("overall", "logs", "orchestrion-list", "x1")) == "orchestrion"
    assert li._completion_bucket_from_path(("overall", "duty", "quest", "5")) == "quest"
    assert li._completion_bucket_from_path(
        ("overall", "character", "adventure-plate", "minion", "348")
    ) == "character/adventure-plate/minion"
    assert li._path_group_key(
        [
            "overall",
            "duty",
            "exploratory-missions",
            "bozja",
            "resistance-rank",
            "x1",
        ]
    ) == "duty/exploratory-missions/bozja/resistance-rank"
    assert li._path_group_key(
        [
            "overall",
            "logs",
            "gathering",
            "gathering-log",
            "mining",
            "level",
            "x9",
        ]
    ) == "logs/gathering/gathering-log/mining/level-based"
    assert li._path_group_key(
        [
            "overall",
            "logs",
            "gathering",
            "gathering-log",
            "mining",
            "level",
            "96-100",
            "x9",
        ]
    ) == "logs/gathering/gathering-log/mining/level-based/96-100"
    assert li._path_group_key(
        [
            "overall",
            "duty",
            "duty-raid-finder",
            "guildhests",
            "archer",
            "43",
        ]
    ) == "duty/duty-raid-finder/guildhests/archer"
    assert li._path_group_key(
        [
            "overall",
            "duty",
            "hall-of-the-novice",
            "tank",
            "7",
        ]
    ) == "duty/hall-of-the-novice/tank"
    assert li._path_group_key(
        [
            "overall",
            "duty",
            "island-sanctuary",
            "crafting",
            "tools",
            "12",
        ]
    ) == "duty/island-sanctuary/crafting/tools"
    assert li._path_group_key(
        [
            "overall",
            "duty",
            "island-sanctuary",
            "isleventory",
            "materials",
            "19",
        ]
    ) == "duty/island-sanctuary/isleventory/materials"
    assert li._path_group_key(
        [
            "overall",
            "duty",
            "collection",
            "portable-archive",
            "the-copied-factory",
            "9",
        ]
    ) == "duty/collection/portable-archive/the-copied-factory"
    assert li._completion_bucket_from_path(
        ("overall", "duty", "collection", "portable-archive", "the-puppets-bunker", "10")
    ) == "duty/collection/portable-archive/the-puppets-bunker"
    assert li._path_group_key(["overall", "custom", "x100"]) == "custom"


def test_completion_payload_starting_class_helpers():
    payload = {"starting-class": "Archer"}
    assert li._completion_payload_starting_class(payload) == "Archer"
    assert li._starting_city_for_class("Archer") == "gridania"
    assert li._starting_city_for_class("Marauder") == "limsa"
    assert li._starting_city_for_class("Thaumaturge") == "uldah"
    assert li._starting_city_for_class(None) is None


def test_bucket_lookup_chain_and_lookup():
    chain = li._bucket_lookup_chain("a/b/c")
    assert chain == ["a/b/c", "a/b", "a"]
    index = {"a": {"5": ("Label",)}}
    labels, bucket = li._lookup_source_labels(index, bucket="a/b", source_id="5")
    assert labels == ("Label",) and bucket == "a"
    # global single-hit fallback
    labels2, bucket2 = li._lookup_source_labels({"z": {"9": ("Solo",)}}, bucket="nope", source_id="9")
    assert labels2 == ("Solo",) and bucket2 == "z"


def test_inline_source_index_and_merge():
    payload = {"custom": {"x804": {"name": "Turali Alumen"}, "x9": "Bare String"}}
    idx = li._build_inline_completion_source_index(payload)
    assert idx["custom"]["804"] == ("Turali Alumen",)
    assert idx["custom"]["9"] == ("Bare String",)

    merged = li._merge_source_indexes({"custom": {"804": ("A",)}}, idx)
    assert "Turali Alumen" in merged["custom"]["804"]
    assert li._merge_source_indexes({"x": {}}, {}) == {"x": {}}


def test_extract_labels():
    # Only an item's canonical name fields are used for matching. Secondary
    # descriptive fields (mob_en, npc_en, description_en, ...) are deliberately
    # ignored: they are generic prose that collides with unrelated rows in the
    # cross-sheet fallback. Every resolvable resource item carries a primary
    # name field, so this loses no recall.
    labels = li._extract_labels({"name_en": "Foo", "mob_en": "Bar", "ignored": 5})
    assert "Foo" in labels
    assert "Bar" not in labels


def test_misc_string_helpers():
    assert li._norm_lookup_key("A'b-C!") == "abc"
    assert li._bucket_tail("a/b/classes-jobs") == "classes-jobs"
    assert li._bucket_tail("") == ""


def test_guildhests_bucket_helpers():
    assert li._guildhests_bucket_from_section("Dark Knight") == "duty/duty-raid-finder/guildhests/dark-knight"
    assert li._guildhests_bucket_from_section("") is None
    assert "duty/duty-raid-finder/guildhests/archer" in li._row_buckets_for_sheet("Guildhests", "Archer")


def test_hall_of_the_novice_role_helpers():
    assert li._hall_of_the_novice_role_key("Tank") == "tank"
    assert li._hall_of_the_novice_role_key("Damage Dealer") == "dps"
    assert li._hall_of_the_novice_bucket_from_role("Healer") == "duty/hall-of-the-novice/healer"
    buckets = li._row_buckets_for_sheet(
        "Hall of the Novice",
        "Hall Of The Novice",
        row_json_obj={"class": "DPS"},
    )
    assert "duty/hall-of-the-novice/dps" in buckets

    craft_buckets = li._row_buckets_for_sheet(
        "Island Sanctuary - Crafting",
        "Tools",
        row_json_obj={"item": "Islekeep's Shovel"},
    )
    assert "duty/island-sanctuary/crafting" in craft_buckets
    assert "duty/island-sanctuary/crafting/tools" in craft_buckets

    rare_animal_buckets = li._row_buckets_for_sheet(
        "Island Sanctuary - Rare Animals",
        "Rare Animals",
        row_json_obj={"name": "Goobue"},
    )
    assert "duty/island-sanctuary/animals" in rare_animal_buckets

    isleventory_buckets = li._row_buckets_for_sheet(
        "Island Sanctuary - Isleventory",
        "Gardening Starters",
        row_json_obj={"item": "Island Runner Bean Seeds"},
    )
    assert "duty/island-sanctuary/isleventory" in isleventory_buckets
    assert "duty/island-sanctuary/isleventory/gardening-starters" in isleventory_buckets

    collection_buckets = li._row_buckets_for_sheet(
        "Collection",
        "The Copied Factory",
        row_json_obj={"entry": "Memory of a Damaged Machine"},
    )
    assert "duty/collection" in collection_buckets
    assert "duty/collection/portable-archive" in collection_buckets
    assert "duty/collection/portable-archive/the-copied-factory" in collection_buckets

    collection_top_buckets = li._row_buckets_for_sheet(
        "Collection",
        "Portable Archive",
        row_json_obj={"entry": "Portable Archive"},
    )
    assert "duty/collection" in collection_top_buckets
    assert "duty/collection/portable-archive" in collection_top_buckets


def test_adventure_plate_section_tag_helpers():
    tags = li._adventure_plate_source_section_tags(
        {
            "decorations": [
                "@PLATE.BASE",
                "@PLATE.TOP_BORDER",
                "@PORTRAIT.ACCENT",
            ]
        }
    )
    assert f"{li._ADVENTURE_PLATE_SECTION_TAG_PREFIX}plate.base" in tags
    assert f"{li._ADVENTURE_PLATE_SECTION_TAG_PREFIX}plate.topborder" in tags
    assert f"{li._ADVENTURE_PLATE_SECTION_TAG_PREFIX}portrait.accent" in tags

    sections = li._adventure_plate_sections_from_labels(sorted(tags))
    assert sections == {"plate.base", "plate.topborder", "portrait.accent"}

    # Portrait backgrounds (the "(Simple)"/"(Ornate)" job plates) route to the
    # Portraits sheet's Background section.
    bg_tags = li._adventure_plate_source_section_tags({"decorations": "@PORTRAIT.BACKGROUND"})
    assert li._adventure_plate_sections_from_labels(sorted(bg_tags)) == {"portrait.background"}

    # Row section keys are sheet-qualified so the two sheets' "Accent" sections
    # do not collide.
    assert li._adventure_plate_row_section_key("Portraits", "Background") == "portrait.background"
    assert li._adventure_plate_row_section_key("Portraits", "Accent") == "portrait.accent"
    assert li._adventure_plate_row_section_key("Adventurer Plate", "Accent") == "plate.accent"
    assert li._adventure_plate_row_section_key("Adventurer Plate", "Pattern Overlay") == "plate.pattern"

    labels = [
        f"{li._ADVENTURE_PLATE_SECTION_TAG_PREFIX}plate.base",
        "Turali Travel Agency",
    ]
    assert li._candidate_match_labels(labels) == ["Turali Travel Agency"]


def test_index_labels_for_bucket_guildhests_and_aetherytes():
    guild_labels = li._index_labels_for_bucket(
        bucket="duty/duty-raid-finder/guildhests/archer",
        node_label="10",
        row_json_obj={"dungeon": "Basic Training: Enemy Parties"},
    )
    assert "Basic Training: Enemy Parties" in guild_labels

    novice_labels = li._index_labels_for_bucket(
        bucket="duty/hall-of-the-novice/tank",
        node_label="Tank",
        row_json_obj={"quest": "Avoid Area of Effect Attacks"},
    )
    assert "Avoid Area of Effect Attacks" in novice_labels

    dungeon_labels = li._index_labels_for_bucket(
        bucket="duty/duty-raid-finder/dungeon",
        node_label="70",
        row_json_obj={"dungeon": "Hells' Lid (Duty)"},
    )
    assert "Hells' Lid (Duty)" in dungeon_labels
    assert "Hells' Lid" in dungeon_labels

    trial_labels = li._index_labels_for_bucket(
        bucket="duty/duty-raid-finder/trial",
        node_label="100",
        row_json_obj={"trial": "Hells' Kier"},
    )
    assert "Hells' Kier" in trial_labels

    aeth_labels = li._index_labels_for_bucket(
        bucket="travel/aetherytes/the-far-east",
        node_label="Kugane",
        row_json_obj={
            "zone_name": "Kugane",
            "type": "Crystal",
            "location_name": "Kugane",
        },
    )
    assert any(label.startswith("@AETHSIG.") for label in aeth_labels)

    aeth_parent_labels = li._index_labels_for_bucket(
        bucket="travel/aetherytes",
        node_label="Kugane",
        row_json_obj={
            "zone_name": "Kugane",
            "type": "Crystal",
            "location_name": "Kugane",
        },
    )
    assert any(label.startswith("@AETHSIG.") for label in aeth_parent_labels)

    aeth_suffix_labels = li._index_labels_for_bucket(
        bucket="travel/aetherytes/others",
        node_label="Old Sharlayan",
        row_json_obj={
            "zone_name": "Old Sharlayan",
            "type": "Crystal",
            "location_name": "Old Sharlayan Aetheryte Plaza",
        },
    )
    assert "@AETHSIG.oldsharlayan.crystal.oldsharlayan" in aeth_suffix_labels


def test_index_labels_for_bucket_island_sanctuary_rank_aliases():
    labels = li._index_labels_for_bucket(
        bucket="duty/island-sanctuary/rank",
        node_label="Islekeep's Stone Hatchet",
        row_json_obj={"sanctuary_rank": "1"},
    )
    assert "1" in labels
    assert "Rank 1" in labels

    labels_int = li._index_labels_for_bucket(
        bucket="duty/island-sanctuary/rank",
        node_label="10",
        row_json_obj={"sanctuary_rank": 10},
    )
    assert "10" in labels_int
    assert "Rank 10" in labels_int


def test_aetheryte_source_signatures():
    item = {
        "name_en": "Kugane",
        "type": "@TYPE.AETHERYTE.CRYSTAL",
        "zone": "@PLACE.KUGANE",
    }
    signatures = li._aetheryte_source_signatures(item)
    assert "@AETHSIG.kugane.crystal.kugane" in signatures

    old_sharlayan = {
        "name_en": "Old Sharlayan Aetheryte Plaza",
        "type": "@TYPE.AETHERYTE.CRYSTAL",
        "zone": "@PLACE.OLD_SHARLAYAN",
    }
    suffix_signatures = li._aetheryte_source_signatures(old_sharlayan)
    assert "@AETHSIG.oldsharlayan.crystal.oldsharlayan" in suffix_signatures


def test_is_quarantined_bucket():
    assert li._is_quarantined_bucket("duty/squadron/squadron")
    assert li._is_quarantined_bucket("duty/squadron/command-missions")
    assert li._is_quarantined_bucket("duty/treasure-hunt/maps")
    assert li._is_quarantined_bucket("duty/treasure-hunt/duties")
    assert li._is_quarantined_bucket("duty/duty-raid-finder/deep-dungeons")
    assert li._is_quarantined_bucket("duty/trust/dt")
    assert not li._is_quarantined_bucket("duty/exploratory-missions/bozja/duties")


def test_is_ignored_untracked_candidate_by_bucket_and_id():
    assert li._is_ignored_untracked_candidate("duty/squadron", "7")
    assert li._is_ignored_untracked_candidate("duty/island-sanctuary/animals", "0")
    assert not li._is_ignored_untracked_candidate("duty/island-sanctuary/animals", "1")
    assert li._is_ignored_untracked_candidate("duty/island-sanctuary/buildings", "10")
    assert li._is_ignored_untracked_candidate("duty/collection", "0")
    assert li._is_ignored_untracked_candidate("logs/crafting-log/armorer", "6202")
    assert li._is_ignored_untracked_candidate("logs/crafting-log/leatherworker", "360")


def test_parse_place_rank_and_current():
    assert li._parse_place_rank("@PLACE.URQOPACHA 3")[1] == 3
    assert li._parse_place_rank("Living Memory 5")[1] == 5
    assert li._parse_place_rank("") is None
    assert li._parse_current_index("@TRAVEL.COMPASS_CURRENT 7") == 7
    assert li._parse_current_index("Aether Current 2") == 2
    assert li._parse_current_index("nonsense") is None


def test_classes_jobs_label_aliases():
    aliases = li._classes_jobs_label_aliases("Scholar / Arcanist")
    assert "Scholar / Arcanist" in aliases
    assert "Scholar" in aliases
    assert "Arcanist" in aliases

    blue_aliases = li._classes_jobs_label_aliases("Blue Mage (Limited Job)")
    assert "Blue Mage" in blue_aliases
    assert "Blue Mage (Limited Job)" in blue_aliases


def test_parse_hunting_labels():
    assert li._parse_hunting_source_label("@CLASS_JOB.GLA 14") == ("gladiator", 14)
    assert li._parse_hunting_source_label("@SOCIETY.ADDER 01") == ("twinadder", 1)
    assert li._parse_hunting_source_label("invalid") is None

    assert li._parse_hunting_workbook_label("Gladiator 14") == ("gladiator", 14)
    assert li._parse_hunting_workbook_label("Twin Adder 01") == ("twinadder", 1)
    assert li._parse_hunting_workbook_label("not-a-rank") is None


def test_filter_hits_for_bucket_scopes_adventure_plate():
    hits = [
        ("Adventurer Plate", 100, "checkbox"),
        ("Minions", 3, "checkbox"),
    ]
    filtered = li._filter_hits_for_bucket("character/adventure-plate/class-job", hits)
    assert filtered == [("Adventurer Plate", 100, "checkbox")]

    filtered_minion = li._filter_hits_for_bucket("character/adventure-plate/minion", hits)
    assert filtered_minion == [("Adventurer Plate", 100, "checkbox")]

    custom_hits = [
        ("Minions", 10, "checkbox"),
        ("Story Quests", 11, "checkbox"),
    ]
    filtered_custom = li._filter_hits_for_bucket("custom", custom_hits)
    assert filtered_custom == [("Story Quests", 11, "checkbox")]

    duty_hits = [
        ("Trials", 52, "checkbox"),
        ("Story Quests", 900, "checkbox"),
    ]
    assert li._filter_hits_for_bucket("quest", duty_hits) == [
        ("Story Quests", 900, "checkbox")
    ]
    assert li._filter_hits_for_bucket("custom", duty_hits) == [
        ("Story Quests", 900, "checkbox")
    ]

    untouched = li._filter_hits_for_bucket("character/relic-gear/lucis-tools", hits)
    assert untouched == hits

    crafting_hits = [
        ("Goldsmithing Log", 1, "checkbox"),
        ("Blacksmithing Log", 2, "checkbox"),
    ]
    assert li._filter_hits_for_bucket(
        "logs/crafting-log/goldsmith",
        crafting_hits,
    ) == [("Goldsmithing Log", 1, "checkbox")]

    mount_speed_hits = [
        ("Mount Speed", 10, "checkbox"),
        ("Aether Currents", 10, "checkbox"),
    ]
    assert li._filter_hits_for_bucket(
        "travel/mount-speed/la-noscea",
        mount_speed_hits,
    ) == [("Mount Speed", 10, "checkbox")]

    opponents_hits = [
        ("Triple Triad Cards", 10, "checkbox"),
        ("Triple Triad Opponents", 10, "checkbox"),
    ]
    assert li._filter_hits_for_bucket(
        "character/gold-saucer/triple-triad-opponents",
        opponents_hits,
    ) == [("Triple Triad Opponents", 10, "checkbox")]

    island_animal_hits = [
        ("Island Sanctuary - Rare Animals", 22, "checkbox"),
        ("Island Sanctuary - Buildings", 22, "checkbox"),
    ]
    assert li._filter_hits_for_bucket(
        "duty/island-sanctuary/animals",
        island_animal_hits,
    ) == [("Island Sanctuary - Rare Animals", 22, "checkbox")]


def test_filter_adventure_plate_hits_by_sections_and_multi_hit_allowance():
    hits = [
        ("Adventurer Plate", 77, "checkbox"),
        ("Adventurer Plate", 302, "checkbox"),
        ("Portraits", 516, "checkbox"),
    ]
    row_sections = {
        ("Adventurer Plate", 77, "checkbox"): "plate.base",
        ("Adventurer Plate", 302, "checkbox"): "plate.topborder",
        ("Portraits", 516, "checkbox"): "portrait.accent",
    }
    labels = [
        "Turali Travel Agency",
        f"{li._ADVENTURE_PLATE_SECTION_TAG_PREFIX}plate.base",
        f"{li._ADVENTURE_PLATE_SECTION_TAG_PREFIX}portrait.accent",
    ]

    filtered = li._filter_adventure_plate_hits_by_sections(
        bucket="character/adventure-plate",
        source_labels=labels,
        hits=hits,
        row_sections=row_sections,
    )
    assert filtered == [
        ("Adventurer Plate", 77, "checkbox"),
        ("Portraits", 516, "checkbox"),
    ]
    assert li._allows_multi_hit_candidate(
        bucket="character/adventure-plate",
        source_labels=labels,
    )
    assert not li._allows_multi_hit_candidate(
        bucket="quest",
        source_labels=labels,
    )


def test_quest_source_path_tags_and_filters():
    tags = li._quest_source_path_token_tags(
        "duty/quest/sidequests/gridanian-sidequests/gridania.json"
    )
    labels = ["An Ill-conceived Venture", *sorted(tags)]
    tokens = li._quest_path_tokens_from_labels(labels)
    assert "gridania" in tokens
    assert "gridaniansidequests" in tokens

    hits = [
        ("Gridanian Sidequests", 21, "checkbox"),
        ("Lominsan Sidequests", 26, "checkbox"),
        ("Ul'dahn Sidequests", 24, "checkbox"),
    ]
    row_context = {
        ("Gridanian Sidequests", 21, "checkbox"): {
            "sheet_name": "Gridanian Sidequests",
            "section_label": "Gridania Quests",
            "label": "An Ill-conceived Venture",
            "row_json_obj": {"quest": "An Ill-conceived Venture", "npc": "Troubled Adventurer"},
        },
        ("Lominsan Sidequests", 26, "checkbox"): {
            "sheet_name": "Lominsan Sidequests",
            "section_label": "Limsa Lominsa Quests",
            "label": "An Ill-conceived Venture",
            "row_json_obj": {"quest": "An Ill-conceived Venture", "npc": "Troubled Adventurer"},
        },
        ("Ul'dahn Sidequests", 24, "checkbox"): {
            "sheet_name": "Ul'dahn Sidequests",
            "section_label": "Ul'Dah Quests",
            "label": "An Ill-conceived Venture",
            "row_json_obj": {"quest": "An Ill-conceived Venture", "npc": "Troubled Adventurer"},
        },
    }

    filtered = li._filter_quest_hits_by_source_tokens(
        bucket="quest",
        source_labels=labels,
        hits=hits,
        row_context=row_context,
        starting_class="Archer",
    )
    assert filtered == [("Gridanian Sidequests", 21, "checkbox")]


def test_quest_source_path_city_token_beats_starting_class_tiebreaker():
    labels = [
        "An Ill-conceived Venture",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}limsa",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}lominsa",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}lominsansidequests",
    ]
    hits = [
        ("Gridanian Sidequests", 21, "checkbox"),
        ("Lominsan Sidequests", 26, "checkbox"),
        ("Ul'dahn Sidequests", 24, "checkbox"),
    ]
    row_context = {
        ("Gridanian Sidequests", 21, "checkbox"): {
            "sheet_name": "Gridanian Sidequests",
            "section_label": "Gridania Quests",
            "label": "An Ill-conceived Venture",
            "row_json_obj": {"quest": "An Ill-conceived Venture"},
        },
        ("Lominsan Sidequests", 26, "checkbox"): {
            "sheet_name": "Lominsan Sidequests",
            "section_label": "Limsa Lominsa Quests",
            "label": "An Ill-conceived Venture",
            "row_json_obj": {"quest": "An Ill-conceived Venture"},
        },
        ("Ul'dahn Sidequests", 24, "checkbox"): {
            "sheet_name": "Ul'dahn Sidequests",
            "section_label": "Ul'Dah Quests",
            "label": "An Ill-conceived Venture",
            "row_json_obj": {"quest": "An Ill-conceived Venture"},
        },
    }

    filtered = li._filter_quest_hits_by_source_tokens(
        bucket="quest",
        source_labels=labels,
        hits=hits,
        row_context=row_context,
        starting_class="Archer",
    )
    assert filtered == [("Lominsan Sidequests", 26, "checkbox")]


def test_quest_starting_class_tiebreaker_for_generic_source_path_tokens():
    labels = [
        "Call of the Sea",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}main",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}mainscenario",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}quests",
    ]
    hits = [
        ("Gridanian Sidequests", 21, "checkbox"),
        ("Lominsan Sidequests", 26, "checkbox"),
        ("Ul'dahn Sidequests", 24, "checkbox"),
    ]
    row_context = {
        ("Gridanian Sidequests", 21, "checkbox"): {
            "sheet_name": "Gridanian Sidequests",
            "section_label": "Gridania Quests",
            "label": "Call of the Sea",
            "row_json_obj": {"quest": "Call of the Sea"},
        },
        ("Lominsan Sidequests", 26, "checkbox"): {
            "sheet_name": "Lominsan Sidequests",
            "section_label": "Limsa Lominsa Quests",
            "label": "Call of the Sea",
            "row_json_obj": {"quest": "Call of the Sea"},
        },
        ("Ul'dahn Sidequests", 24, "checkbox"): {
            "sheet_name": "Ul'dahn Sidequests",
            "section_label": "Ul'Dah Quests",
            "label": "Call of the Sea",
            "row_json_obj": {"quest": "Call of the Sea"},
        },
    }

    filtered = li._filter_quest_hits_by_source_tokens(
        bucket="quest",
        source_labels=labels,
        hits=hits,
        row_context=row_context,
        starting_class="Archer",
    )
    assert filtered == [("Gridanian Sidequests", 21, "checkbox")]


def test_quest_source_path_filter_ignores_generic_tokens():
    labels = [
        "Blood in the Water",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}company",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}companyleves",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}immortal",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}flames",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}levequests",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}leves",
    ]
    hits = [
        ("Company Leves", 49, "checkbox"),
        ("Fishing Leves", 92, "checkbox"),
    ]
    row_context = {
        ("Company Leves", 49, "checkbox"): {
            "sheet_name": "Company Leves",
            "section_label": "Immortal Flames Levequests",
            "label": "Blood in the Water",
            "row_json_obj": {"name": "Blood in the Water", "type": "Equity"},
        },
        ("Fishing Leves", 92, "checkbox"): {
            "sheet_name": "Fishing Leves",
            "section_label": "Kugane",
            "label": "Blood in the Water",
            "row_json_obj": {"name": "Blood in the Water", "type": "Concord"},
        },
    }

    filtered = li._filter_quest_hits_by_source_tokens(
        bucket="quest",
        source_labels=labels,
        hits=hits,
        row_context=row_context,
        starting_class=None,
    )
    assert filtered == [("Company Leves", 49, "checkbox")]


def test_quest_source_path_filter_prefers_highest_token_overlap():
    labels = [
        "Simply the Hest",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}uldahnsidequests",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}western",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}westernthanalan",
        f"{li._QUEST_PATH_TOKEN_TAG_PREFIX}thanalan",
    ]
    hits = [
        ("Lominsan Sidequests", 69, "checkbox"),
        ("Ul'dahn Sidequests", 44, "checkbox"),
    ]
    row_context = {
        ("Lominsan Sidequests", 69, "checkbox"): {
            "sheet_name": "Lominsan Sidequests",
            "section_label": "Western La Noscea Quests",
            "label": "Simply the Hest",
            "row_json_obj": {"quest": "Simply the Hest"},
        },
        ("Ul'dahn Sidequests", 44, "checkbox"): {
            "sheet_name": "Ul'dahn Sidequests",
            "section_label": "Western Thanalan Quests",
            "label": "Simply the Hest",
            "row_json_obj": {"quest": "Simply the Hest"},
        },
    }

    filtered = li._filter_quest_hits_by_source_tokens(
        bucket="quest",
        source_labels=labels,
        hits=hits,
        row_context=row_context,
        starting_class="Archer",
    )
    assert filtered == [("Ul'dahn Sidequests", 44, "checkbox")]


def test_island_sanctuary_and_gathering_filters():
    island_hits = [
        ("Island Sanctuary - Buildings", 37, "checkbox"),
        ("Island Sanctuary - Buildings", 38, "checkbox"),
        ("Island Sanctuary - Buildings", 39, "checkbox"),
        ("Island Sanctuary - Buildings", 40, "checkbox"),
    ]
    island_filtered = li._filter_island_sanctuary_hits(
        bucket="duty/island-sanctuary/buildings",
        hits=island_hits,
        source_state="value",
        source_value=2.0,
    )
    assert island_filtered == [
        ("Island Sanctuary - Buildings", 37, "checkbox"),
        ("Island Sanctuary - Buildings", 38, "checkbox"),
    ]

    animal_hits = [
        ("Island Sanctuary - Rare Animals", 52, "checkbox"),
        ("Island Sanctuary - Buildings", 52, "checkbox"),
    ]
    animal_filtered = li._filter_island_sanctuary_hits(
        bucket="duty/island-sanctuary/animals",
        hits=animal_hits,
        source_state="done",
        source_value=None,
    )
    assert animal_filtered == [("Island Sanctuary - Rare Animals", 52, "checkbox")]

    gathering_hits = [
        ("Botanist Logs", 115, "checkbox"),
        ("Botanist Logs", 523, "checkbox"),
    ]
    row_context = {
        ("Botanist Logs", 115, "checkbox"): {
            "sheet_name": "Botanist Logs",
            "section_label": "Levels 46-50",
            "label": "Dark Matter Cluster",
            "row_json_obj": {"type": "Logging"},
        },
        ("Botanist Logs", 523, "checkbox"): {
            "sheet_name": "Botanist Logs",
            "section_label": "Levels 46-50",
            "label": "Dark Matter Cluster",
            "row_json_obj": {"type": "Harvesting"},
        },
    }
    filtered_logging = li._filter_gathering_log_hits_by_type(
        bucket="logs/gathering/gathering-log/logging/level-based/96-100",
        hits=gathering_hits,
        row_context=row_context,
    )
    assert filtered_logging == [("Botanist Logs", 115, "checkbox")]

    crafting_hits = [
        ("Culinary Log", 4, "checkbox"),
        ("Culinary Log", 5, "checkbox"),
    ]
    crafting_row_context = {
        ("Culinary Log", 4, "checkbox"): {
            "sheet_name": "Culinary Log",
            "section_label": "Levels 81-85",
            "label": "Dark Rye Flour",
            "row_json_obj": {"item": "Dark Rye Flour", "mat_1": "Rye Flour"},
        },
        ("Culinary Log", 5, "checkbox"): {
            "sheet_name": "Culinary Log",
            "section_label": "Levels 81-85",
            "label": "Rye Bread",
            "row_json_obj": {"item": "Rye Bread", "mat_1": "Dark Rye Flour"},
        },
    }
    filtered_crafting = li._filter_crafting_log_hits(
        bucket="logs/crafting-log/culinarian",
        match_labels=["Dark Rye Flour"],
        hits=crafting_hits,
        row_context=crafting_row_context,
    )
    assert filtered_crafting == [("Culinary Log", 4, "checkbox")]

    ingredient_only = li._filter_crafting_log_hits(
        bucket="logs/crafting-log/culinarian",
        match_labels=["Dark Rye Flour"],
        hits=[("Culinary Log", 5, "checkbox")],
        row_context=crafting_row_context,
    )
    assert ingredient_only == []


def test_remap_crafting_log_cross_bucket_hits_job_to_shared():
    label = "Magitek Repair Materials"
    norm = li._norm_label(label)
    entry = ("Shared Craft Log", 101, "checkbox")
    exact_idx = {
        "logs/crafting-log/armorer": {},
        "logs/crafting-log/shared": {label.casefold(): [entry]},
    }
    norm_idx = {
        "logs/crafting-log/armorer": {},
        "logs/crafting-log/shared": {norm: [entry]},
    }
    row_context = {
        entry: {
            "label": label,
            "row_json_obj": {"item": label},
        }
    }

    remapped = li._remap_crafting_log_cross_bucket_hits(
        bucket="logs/crafting-log/armorer",
        aliases=[label],
        match_labels=[label],
        exact_idx=exact_idx,
        norm_idx=norm_idx,
        row_context=row_context,
    )
    assert remapped == [entry]


def test_remap_crafting_log_cross_bucket_hits_shared_to_job_family():
    label = "Amaro Barding Repair Materials"
    norm = li._norm_label(label)
    entries = [
        ("Carpentry Log", 301, "checkbox"),
        ("Leatherworking Log", 401, "checkbox"),
        ("Weaving Log", 501, "checkbox"),
    ]
    exact_idx = {
        "logs/crafting-log/shared": {},
        "logs/crafting-log/carpenter": {label.casefold(): [entries[0]]},
        "logs/crafting-log/leatherworker": {label.casefold(): [entries[1]]},
        "logs/crafting-log/weaver": {label.casefold(): [entries[2]]},
    }
    norm_idx = {
        "logs/crafting-log/shared": {},
        "logs/crafting-log/carpenter": {norm: [entries[0]]},
        "logs/crafting-log/leatherworker": {norm: [entries[1]]},
        "logs/crafting-log/weaver": {norm: [entries[2]]},
    }
    row_context = {
        entry: {
            "label": label,
            "row_json_obj": {"item": label},
        }
        for entry in entries
    }

    remapped = li._remap_crafting_log_cross_bucket_hits(
        bucket="logs/crafting-log/shared",
        aliases=[label],
        match_labels=[label],
        exact_idx=exact_idx,
        norm_idx=norm_idx,
        row_context=row_context,
    )
    assert remapped is not None
    assert set(remapped) == set(entries)


def test_allows_multi_hit_candidate_for_safe_crafting_shared_family():
    safe_hits = [
        ("Carpentry Log", 301, "checkbox"),
        ("Leatherworking Log", 401, "checkbox"),
        ("Weaving Log", 501, "checkbox"),
    ]
    assert li._allows_multi_hit_candidate(
        bucket="logs/crafting-log/shared",
        source_labels=["Amaro Barding Repair Materials"],
        hits=safe_hits,
    )

    unsafe_hits = [
        ("Carpentry Log", 301, "checkbox"),
        ("Weaving Log", 501, "checkbox"),
    ]
    assert not li._allows_multi_hit_candidate(
        bucket="logs/crafting-log/shared",
        source_labels=["Amaro Barding Repair Materials"],
        hits=unsafe_hits,
    )


def test_candidate_aliases_for_crafting_typo_variants():
    alch = li._candidate_aliases("logs/crafting-log/alchemist", "Grade 3 Tincture of Dexterity")
    assert "Grade 3 Tinctures of Dexterity" in alch

    blacksmith = li._candidate_aliases("logs/crafting-log/blacksmith", "Titanbronze Fists")
    assert "Titanbronze Fist" in blacksmith

    leather = li._candidate_aliases(
        "logs/crafting-log/leatherworker",
        "Rarefied Crocodileskin Leggings",
    )
    assert "Rarefied Crocodileskin Leggins" in leather


def test_filter_crafting_log_hits_stat_affix_for_truncated_labels():
    hits = [
        ("Goldsmithing Log", 180, "checkbox"),
        ("Goldsmithing Log", 182, "checkbox"),
        ("Goldsmithing Log", 183, "checkbox"),
        ("Goldsmithing Log", 184, "checkbox"),
    ]
    row_context = {
        ("Goldsmithing Log", 180, "checkbox"): {
            "sheet_name": "Goldsmithing Log",
            "section_label": "Levels 86-90",
            "label": "Star Quartz Wristband of",
            "row_json_obj": {"item": "Star Quartz Wristband of", "mat_3": "Grade 5 Vitality Alkahest"},
        },
        ("Goldsmithing Log", 182, "checkbox"): {
            "sheet_name": "Goldsmithing Log",
            "section_label": "Levels 86-90",
            "label": "Star Quartz Wristband of",
            "row_json_obj": {"item": "Star Quartz Wristband of", "mat_3": "Grade 5 Dexterity Alkahest"},
        },
        ("Goldsmithing Log", 183, "checkbox"): {
            "sheet_name": "Goldsmithing Log",
            "section_label": "Levels 86-90",
            "label": "Star Quartz Wristband of",
            "row_json_obj": {"item": "Star Quartz Wristband of", "mat_3": "Grade 5 Intelligence Alkahest"},
        },
        ("Goldsmithing Log", 184, "checkbox"): {
            "sheet_name": "Goldsmithing Log",
            "section_label": "Levels 86-90",
            "label": "Star Quartz Wristband of",
            "row_json_obj": {"item": "Star Quartz Wristband of", "mat_3": "Grade 5 Mind Alkahest"},
        },
    }

    aiming = li._filter_crafting_log_hits(
        bucket="logs/crafting-log/goldsmith",
        match_labels=["Star Quartz Wristband of Aiming"],
        hits=hits,
        row_context=row_context,
    )
    assert aiming == [("Goldsmithing Log", 182, "checkbox")]

    casting = li._filter_crafting_log_hits(
        bucket="logs/crafting-log/goldsmith",
        match_labels=["Star Quartz Wristband of Casting"],
        hits=hits,
        row_context=row_context,
    )
    assert casting == [("Goldsmithing Log", 183, "checkbox")]


def test_aether_zone_from_path():
    # zone label sits three positions after the "travel" segment
    parts = ("travel", "aether-currents", "endwalker", "the-sea-of-clouds", "x1")
    assert li._aether_zone_from_path(parts) == li._norm_lookup_key("the sea of clouds")
    assert li._aether_zone_from_path(("a", "b")) is None


def test_dedupe_hits_and_partial_generic():
    assert li._dedupe_hits([("S", 1, "x"), ("S", 1, "x")]) == [("S", 1, "x")]
    assert li._dedupe_hits(None) is None
    idx = {li._norm_label("Quest Alpha Long"): [("Story Quests", 3, "checkbox")]}
    hits = li._partial_match_hits_generic(["Quest Alpha Longg"], idx, cutoff=0.8)
    assert hits == [("Story Quests", 3, "checkbox")]
    assert li._partial_match_hits_generic(["x"], {}) is None


def test_generic_label_aliases_goobue_spelling_variant():
    aliases = li._generic_label_aliases("Goobbue")
    assert "Goobue" in aliases


def test_partial_match_hits_quest_multiword_still_allowed():
    idx = {li._norm_label("School of Hard Knocks"): [("Story Quests", 9, "checkbox")]}
    hits = li._partial_match_hits(
        bucket="quest",
        aliases=["School of Hard Nocks"],
        bucket_norm=idx,
        bucket_norm_keys=list(idx.keys()),
    )
    assert hits == [("Story Quests", 9, "checkbox")]


def test_partial_match_hits_quest_blocks_single_word_job_collision():
    idx = {li._norm_label("Gunbreaker"): [("Relic Weapons", 557, "checkbox")]}
    hits = li._partial_match_hits(
        bucket="quest",
        aliases=["Unbreaker"],
        bucket_norm=idx,
        bucket_norm_keys=list(idx.keys()),
    )
    assert hits is None


def test_format_exception():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        text = li.format_exception(exc)
    assert "ValueError" in text and "boom" in text


def test_load_payload_validation(tmp_path):
    good = tmp_path / "g.json"
    good.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert li.load_payload(good) == {"a": 1}

    bad = tmp_path / "b.json"
    bad.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError):
        li.load_payload(bad)

    comp = tmp_path / "c.json"
    comp.write_text(json.dumps({"overall": {}}), encoding="utf-8")
    assert li.load_completion_payload(comp)["overall"] == {}
    comp.write_text(json.dumps({"no_overall": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        li.load_completion_payload(comp)


# --- resource-root / completion-path discovery ------------------------------

def test_resource_root_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert li._resource_root_candidates() == []
    assert li.resolve_resource_root() is None

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert li.resolve_resource_root() is None  # dir doesn't exist yet
    target = tmp_path / "Programs" / "ffxiv-completionist" / "resources" / "resources"
    target.mkdir(parents=True)
    assert li.resolve_resource_root() == target


def test_completion_path_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA", raising=False)
    assert li.default_completion_path() is None
    assert li.list_detected_completion_files() == []

    monkeypatch.setenv("APPDATA", str(tmp_path))
    base = tmp_path / "ffxiv-completionist"
    base.mkdir(parents=True)
    (base / "completion.json").write_text("{}", encoding="utf-8")
    (base / "alt-completion.json").write_text("{}", encoding="utf-8")
    found = li.list_detected_completion_files()
    assert any(p.name == "completion.json" for p in found)


def test_resource_bucket_for_path():
    assert li._resource_bucket_for_path("logs/orchestrion-list/x.json") == "orchestrion"
    assert li._resource_bucket_for_path("duty/quest/1.json") == "quest"
    assert li._resource_bucket_for_path("character/mount-guide.json") == "mount"


# --- end-to-end imports -----------------------------------------------------

def test_import_lodestone_payload_matches_and_reports(conn, character_id, tmp_path):
    connection, _ = conn
    payload = {
        "authenticated_pages": {
            "quest": {
                "entries": [
                    {"title": "Quest Alpha"},     # done baseline -> skipped
                    {"title": "Quest Beta"},      # todo -> applied done
                    {"title": "Totally Made Up Quest"},  # unmatched
                ]
            }
        }
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    logs: list[str] = []
    summary = li.import_lodestone_payload(
        connection, character_id=character_id, payload_path=path, progress=logs.append
    )
    assert summary.matched_candidates == 2
    assert summary.rows_applied == 1
    assert summary.rows_skipped_already_done == 1
    assert summary.unmatched_candidates == 1
    assert summary.unmatched_items[0]["bucket"] == "quest"
    assert logs

    from app import db
    assert db.effective_state(connection, character_id, _run(connection), "Story Quests", 4) == "done"


def test_import_lodestone_payload_clear_existing(conn, character_id, tmp_path):
    connection, run_id = conn
    from app import db

    # Pre-set an override that clear_existing should wipe before re-import.
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")
    payload = {"authenticated_pages": {"quest": {"entries": [{"title": "Quest Beta"}]}}}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    li.import_lodestone_payload(
        connection, character_id=character_id, payload_path=path, clear_existing=True
    )
    # The wiped override reverted Side Stuff row 5 back to baseline (todo).
    assert db.effective_state(connection, character_id, run_id, "Side Stuff", 5) == "todo"


def test_import_desktop_completion(conn, character_id, tmp_path, monkeypatch):
    connection, run_id = conn
    from app import db

    monkeypatch.setattr(li, "resolve_resource_root", lambda: tmp_path)
    payload = {
        "overall": {"custom": {"x100": "Y", "x200": 100, "x300": "X"}},
        "custom": {
            "x100": {"name": "Quest Beta"},     # done -> applied
            "x200": {"name": "Quest Gamma"},    # value 100 on checkbox -> done
            "x300": {"name": "Thing Two"},      # excluded; already excluded -> skip
        },
    }
    path = tmp_path / "completion.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    logs: list[str] = []
    summary = li.import_desktop_completion(
        connection, character_id=character_id, completion_path=path, progress=logs.append
    )
    assert summary.matched_candidates == 3
    assert summary.rows_applied == 2
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 4) == "done"
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 5) == "done"


def test_import_desktop_completion_skips_ambiguous_multi_hit(
    conn,
    character_id,
    tmp_path,
    monkeypatch,
):
    connection, run_id = conn
    from app import db

    monkeypatch.setattr(li, "resolve_resource_root", lambda: tmp_path)

    payload = {
        "overall": {"custom": {"x100": "Y"}},
        "custom": {"x100": {"name": "Quest Beta"}},
    }
    path = tmp_path / "completion.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    original_filter = li._filter_hits_for_bucket

    def force_ambiguous(bucket: str, hits):
        filtered = original_filter(bucket, hits)
        if bucket == "custom" and filtered:
            return [
                ("Story Quests", 4, "checkbox"),
                ("Side Stuff", 5, "checkbox"),
            ]
        return filtered

    monkeypatch.setattr(li, "_filter_hits_for_bucket", force_ambiguous)

    summary = li.import_desktop_completion(
        connection,
        character_id=character_id,
        completion_path=path,
    )

    assert summary.matched_candidates == 0
    assert summary.unmatched_candidates == 1
    assert summary.unmatched_items[0]["reason"] == "ambiguous_multi_hit"
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 4) == "todo"


def test_import_desktop_completion_classes_jobs_prefers_label_match(
    conn,
    character_id,
    tmp_path,
    monkeypatch,
):
    connection, run_id = conn

    monkeypatch.setattr(li, "resolve_resource_root", lambda: tmp_path)
    monkeypatch.setattr(
        li,
        "_build_source_label_index",
        lambda _root: {
            "character/character/classes-jobs": {
                # Intentionally swapped source ids to ensure import does not
                # rely on positional order when labels are available.
                "0": ("Warrior / Marauder",),
                "1": ("Paladin / Gladiator",),
            }
        },
    )

    payload = {
        "overall": {
            "character": {
                "character": {
                    "classes--jobs": {
                        "0": "10.25",
                        "1": "20.75",
                    }
                }
            }
        }
    }
    path = tmp_path / "completion.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    summary = li.import_desktop_completion(
        connection,
        character_id=character_id,
        completion_path=path,
    )
    assert summary.matched_candidates == 2

    values = connection.execute(
        """
        SELECT row_index, progress_percent
        FROM character_progress
        WHERE character_id = ? AND run_id = ? AND sheet_name = 'Classes-Jobs'
          AND row_index IN (3, 4)
        ORDER BY row_index
        """,
        (character_id, run_id),
    ).fetchall()
    by_row = {int(row["row_index"]): float(row["progress_percent"]) for row in values}
    assert by_row[3] == 20.75  # Paladin
    assert by_row[4] == 10.25  # Warrior


def test_reset_character_progress(conn, character_id):
    connection, run_id = conn
    from app import db, progress_io

    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")
    character = db.get_character(connection, character_id)
    li.reset_character_progress(connection, character, run_id)

    count = connection.execute(
        "SELECT COUNT(*) AS c FROM character_progress WHERE character_id = ?",
        (character_id,),
    ).fetchone()["c"]
    assert count == 0
    doc = progress_io.load_sidecar(progress_io.sidecar_path(character["name"]))
    assert doc["progress"] == []


def _run(connection):
    from app import db

    return db.latest_run_id(connection)
