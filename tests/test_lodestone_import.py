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


def test_quest_label_aliases():
    aliases = li._quest_label_aliases("Abalathian Sidequests (A Cropper's Duty)")
    assert "A Cropper's Duty" in aliases
    assert any("Abalathian" in a for a in aliases)


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
    assert "minion" in li._sheet_buckets("Minion Guide")
    assert li._sheet_buckets("Triple Triad Cards") == {"tripletriad"}
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
    assert li._path_group_key(["overall", "custom", "x100"]) == "custom"


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
    labels = li._extract_labels({"name_en": "Foo", "mob_en": "Bar", "ignored": 5})
    assert "Foo" in labels and "Bar" in labels


def test_misc_string_helpers():
    assert li._norm_lookup_key("A'b-C!") == "abc"
    assert li._bucket_tail("a/b/classes-jobs") == "classes-jobs"
    assert li._bucket_tail("") == ""


def test_parse_place_rank_and_current():
    assert li._parse_place_rank("@PLACE.URQOPACHA 3")[1] == 3
    assert li._parse_place_rank("Living Memory 5")[1] == 5
    assert li._parse_place_rank("") is None
    assert li._parse_current_index("@TRAVEL.COMPASS_CURRENT 7") == 7
    assert li._parse_current_index("Aether Current 2") == 2
    assert li._parse_current_index("nonsense") is None


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
