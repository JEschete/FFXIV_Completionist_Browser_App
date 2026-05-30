"""Extra db.py coverage: value caps, chains, search/export, nav helpers,
character CRUD, and section grouping."""
from __future__ import annotations

import pytest

from app import db


# --- value caps -------------------------------------------------------------

def test_default_value_cap():
    assert db._default_value_cap("Classes-Jobs", "Desynthesis", "Smith") == 770.0
    assert db._default_value_cap("Classes-Jobs", "Magic", "Blue Mage") == 80.0
    assert db._default_value_cap("Other", "Sec", "Label") == 100.0


def test_value_cap_override_roundtrip(ingested_db):
    key = db._value_cap_key("Classes-Jobs", "Tanks", "Paladin")
    saved = db.save_value_cap_overrides({key: 150.0, "bad": -1})
    assert saved == {key: 150.0}                  # non-positive dropped
    assert db.load_value_cap_overrides()[key] == 150.0
    assert db.resolve_value_cap("Classes-Jobs", "Tanks", "Paladin") == 150.0
    # cache hit path (mtime unchanged)
    assert db.load_value_cap_overrides()[key] == 150.0


def test_load_value_caps_missing_and_corrupt(ingested_db):
    assert db.load_value_cap_overrides() == {}      # file absent
    db.VALUE_CAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.VALUE_CAPS_PATH.write_text("not json", encoding="utf-8")
    db._VALUE_CAPS_CACHE_MTIME_NS = None
    assert db.load_value_cap_overrides() == {}      # invalid -> {}


def test_value_row_cap(conn):
    connection, run_id = conn
    assert db.value_row_cap(connection, run_id, "Classes-Jobs", 3) == 100.0
    # a checkbox row (non-value) returns the flat 100 default
    assert db.value_row_cap(connection, run_id, "Side Stuff", 3) == 100.0


def test_classes_jobs_cap_rows(conn):
    connection, run_id = conn
    rows = db.classes_jobs_cap_rows(connection, run_id)
    names = {r["display_name"] for r in rows}
    assert {"Paladin", "Warrior"} <= names
    for r in rows:
        assert r["default_cap"] == 100 and "cap_key" in r


# --- search / export / snapshot ---------------------------------------------

def test_search_nodes(conn, character_id):
    connection, run_id = conn
    assert db.search_nodes(connection, run_id, character_id, "a") == []   # too short
    hits = db.search_nodes(connection, run_id, character_id, "Quest")
    labels = {h["label"] for h in hits}
    assert "Quest Alpha" in labels

    sheet_hits = db.search_nodes(connection, run_id, character_id, "Character")
    assert any(
        h.get("result_kind") == "sheet" and h.get("label") == "Character Menu"
        for h in sheet_hits
    )

    section_hits = db.search_nodes(connection, run_id, character_id, "MAIN STORY")
    assert any(
        h.get("result_kind") == "section" and h.get("label") == "MAIN STORY CHAIN"
        for h in section_hits
    )


def test_fetch_export_rows(conn, character_id):
    connection, run_id = conn
    rows = db.fetch_export_rows(connection, run_id, character_id)
    labels = {r["label"] for r in rows}
    assert "Thing One" in labels and "Quest Alpha" in labels
    # sections are excluded from the export
    assert all(r["label"] not in {"MAIN STORY CHAIN", "ODDS AND ENDS"} for r in rows)


def test_snapshot_trackable_rows(conn, character_id):
    connection, run_id = conn
    snap = db.snapshot_trackable_rows(connection, run_id, character_id)
    assert ("Story Quests", 3) in snap
    assert snap[("Story Quests", 3)]["state"] == "done"
    assert snap[("Side Stuff", 5)]["state"] == "todo"


# --- chains -----------------------------------------------------------------

def test_fetch_chain(conn, character_id):
    connection, run_id = conn
    chain = db.fetch_chain(connection, run_id, character_id, "Story Quests", 5)
    prereq_rows = {p["row_index"] for p in chain["prereqs"]}
    assert prereq_rows == {3, 4}          # Gamma's path is Alpha -> Beta
    assert chain["blocked"] is True       # Beta not done yet


def test_chain_sheets_overview(conn, character_id):
    connection, run_id = conn
    # Story Quests has only 2 sequence links (< 3 threshold) -> not listed.
    overview = db.chain_sheets_overview(connection, run_id, character_id)
    assert isinstance(overview, list)


def test_sheet_chain_flags(conn, character_id):
    connection, run_id = conn
    flags = db.sheet_chain_flags(connection, run_id, character_id, "Story Quests")
    # Quest Beta (row 4) has one prerequisite (Alpha) and is currently blocked? Alpha is done.
    assert flags[4]["prereqs"] == 1
    assert flags[4]["blocked"] is False   # Alpha is done baseline


def test_fetch_row_value_cap(conn, character_id):
    connection, run_id = conn
    row = db.fetch_row(connection, run_id, character_id, "Classes-Jobs", 3)
    assert row["label"] == "Paladin" and row["value_cap"] == 100.0
    assert db.fetch_row(connection, run_id, character_id, "Classes-Jobs", 9999) is None


# --- nav tree / grouping ----------------------------------------------------

def test_build_nav_tree_and_breadcrumbs(conn, character_id):
    connection, run_id = conn
    sheets = db.fetch_all_sheets(connection, run_id)
    rollups = db.sheet_rollups(connection, run_id, character_id)
    roots, overall, by_name = db.build_nav_tree(sheets, rollups)
    assert any(r["sheet_name"] == "Character Menu" for r in roots)

    trackable_rows = connection.execute(
        """
        SELECT sheet_name, row_index, row_type
        FROM nodes
        WHERE run_id = ? AND row_type IN ('checkbox', 'value')
        """,
        (run_id,),
    ).fetchall()
    expected_total = 0
    for row in trackable_rows:
        row_type = str(row["row_type"] or "checkbox")
        if row_type == "value":
            expected_total += int(
                float(db.value_row_cap(connection, run_id, row["sheet_name"], int(row["row_index"])))
            )
        else:
            expected_total += 1
    assert overall["total"] == expected_total

    crumbs = db.breadcrumb_path(by_name, "Story Quests")
    assert [c["sheet_name"] for c in crumbs] == ["Character Menu", "Story Quests"]

    node = db.find_node(roots, "Story Quests")
    assert node is not None
    db.mark_active_path(roots, "Story Quests")
    menu = db.find_node(roots, "Character Menu")
    assert menu["has_active"] is True


def test_group_rows_by_section(conn, character_id):
    connection, run_id = conn
    rows = db.fetch_rows(connection, run_id, character_id, "Story Quests")
    groups = db.group_rows_by_section(rows)
    assert groups[0]["section"] == "Main Story Chain"
    assert {r["label"] for r in groups[0]["rows"]} == {"Quest Alpha", "Quest Beta", "Quest Gamma"}
    assert db.sheet_supports_section_sort("Story Quests") is False


def test_fetch_rows_filters(conn, character_id):
    connection, run_id = conn
    done = db.fetch_rows(connection, run_id, character_id, "Side Stuff", state="done")
    # sections always kept; only the done trackable row (Thing One) remains
    labels = {r["label"] for r in done if not r["is_section"]}
    assert labels == {"Thing One"}
    # query filter keeps the banner but narrows rows
    q = db.fetch_rows(connection, run_id, character_id, "Side Stuff", q="Thing Three")
    assert {r["label"] for r in q if not r["is_section"]} == {"Thing Three"}


# --- character CRUD ---------------------------------------------------------

def test_create_and_delete_character(conn):
    connection, _ = conn
    with pytest.raises(ValueError):
        db.create_character(connection, "   ")     # blank name rejected
    new_id = db.create_character(connection, "Second")
    assert db.get_character(connection, new_id)["name"] == "Second"

    # deleting down to one is fine; deleting the last raises
    db.delete_character(connection, new_id)
    with pytest.raises(ValueError):
        db.delete_character(connection, 1)         # only one left


def test_rename_character_preserves_progress_and_migrates_sidecar(conn, character_id):
    connection, run_id = conn
    from app import progress_io

    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")

    old_name = str(db.get_character(connection, character_id)["name"])
    old_sidecar = progress_io.sidecar_path(old_name)
    assert old_sidecar.exists()

    db.rename_character(connection, character_id, "Renamed Adventurer")

    renamed = db.get_character(connection, character_id)
    assert renamed is not None
    assert renamed["name"] == "Renamed Adventurer"
    assert db.effective_state(connection, character_id, run_id, "Side Stuff", 5) == "done"

    new_sidecar = progress_io.sidecar_path("Renamed Adventurer")
    assert new_sidecar.exists()
    doc = progress_io.load_sidecar(new_sidecar)
    assert isinstance(doc, dict)
    assert str((doc.get("character") or {}).get("name") or "") == "Renamed Adventurer"
    if new_sidecar != old_sidecar:
        assert not old_sidecar.exists()


def test_rename_character_validation(conn, character_id):
    connection, _ = conn
    second_id = db.create_character(connection, "Second Rename Target", "GLADIATOR")

    with pytest.raises(ValueError):
        db.rename_character(connection, character_id, "   ")

    with pytest.raises(ValueError):
        db.rename_character(connection, second_id, db.get_character(connection, character_id)["name"])


def test_set_character_class_validation(conn, character_id):
    connection, _ = conn
    with pytest.raises(ValueError):
        db.set_character_class(connection, character_id, "NOTACLASS")
    db.set_character_class(connection, character_id, "GLADIATOR")
    assert db.get_character(connection, character_id)["starting_class"] == "GLADIATOR"
    db.set_character_class(connection, character_id, None)   # clearing is allowed


def test_resolve_active_character(conn):
    connection, _ = conn
    # unknown requested id falls back to the first character
    row = db.resolve_active_character(connection, 99999)
    assert row["id"] == 1
    # explicit valid id is honored
    assert db.resolve_active_character(connection, 1)["id"] == 1


def test_clear_row_override_without_override(conn, character_id):
    connection, run_id = conn
    # No explicit override exists yet -> returns False.
    assert db.clear_row_override(connection, character_id, run_id, "Side Stuff", 3) is False
