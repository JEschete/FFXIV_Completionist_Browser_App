"""Phase 1 — sidecar identity + write-through/reconcile round-trips.

Two invariants matter most here:
  1. The content hash computed at ingest (prep._row_hash) equals the one the
     running app computes (progress_io._hash_row) for the same row. The code
     comments call this out as a cross-module contract.
  2. A state change written through to the sidecar survives a full
     reconcile_all rebuild of the DB.
"""
from __future__ import annotations

import json

import prep_xlsx_to_sqlite as prep

from app import db, progress_io


def test_hash_invariant_prep_matches_progress_io(conn):
    """stable_hash stored at ingest must match progress_io's hash of row_json."""
    connection, run_id = conn
    rows = connection.execute(
        "SELECT row_json, stable_hash FROM nodes "
        "WHERE run_id = ? AND row_type != 'section'",
        (run_id,),
    ).fetchall()
    assert rows
    for r in rows:
        # app-side hash of the stored JSON
        assert progress_io._hash_row(r["row_json"]) == r["stable_hash"]
        # prep-side hash of the parsed dict (the original generator)
        assert prep._row_hash(json.loads(r["row_json"])) == r["stable_hash"]


def test_compute_stable_ids_tiers():
    ids = progress_io.compute_stable_ids(
        "Story Quests", "Main Story Chain", "Quest Beta",
        json.dumps({"quest": "Quest Beta"}), 4,
    )
    assert ids["section_label"] == "sheet:Story Quests|section:Main Story Chain|label:Quest Beta"
    assert ids["label"] == "sheet:Story Quests|label:Quest Beta"
    assert ids["hash"].startswith("sheet:Story Quests|hash:")
    assert ids["position"] == "sheet:Story Quests|row:4"


def test_write_through_creates_sidecar(conn, character_id):
    connection, run_id = conn
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")

    name = db.get_character(connection, character_id)["name"]
    path = progress_io.sidecar_path(name)
    assert path.exists()

    doc = json.loads(path.read_text(encoding="utf-8"))
    states = {
        entry["ids"]["position"]: entry["state"]
        for entry in doc["progress"]
        if "position" in entry.get("ids", {})
    }
    assert states.get("sheet:Side Stuff|row:5") == "done"


def test_remove_state_change_clears_sidecar_entry(conn, character_id):
    connection, run_id = conn
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")
    removed = db.clear_row_override(connection, character_id, run_id, "Side Stuff", 5)
    assert removed is True

    name = db.get_character(connection, character_id)["name"]
    doc = json.loads(progress_io.sidecar_path(name).read_text(encoding="utf-8"))
    positions = {e.get("ids", {}).get("position") for e in doc["progress"]}
    assert "sheet:Side Stuff|row:5" not in positions


def test_sidecar_reconcile_round_trip(conn, character_id):
    """Toggle a couple of rows, wipe the DB overrides, then replay the sidecar
    and confirm the effective states come back identical."""
    connection, run_id = conn
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")
    db.set_row_state(connection, character_id, run_id, "Story Quests", 4, "excluded")
    connection.commit()

    # Drop the DB-side overrides; the sidecar is the source of truth.
    connection.execute(
        "DELETE FROM character_progress WHERE character_id = ?", (character_id,)
    )
    connection.execute("DELETE FROM progress_rollup WHERE character_id = ?", (character_id,))
    connection.commit()
    assert db.effective_state(connection, character_id, run_id, "Side Stuff", 5) == "todo"

    report = progress_io.reconcile_all(connection, run_id)
    assert report.total_orphaned() == 0

    assert db.effective_state(connection, character_id, run_id, "Side Stuff", 5) == "done"
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 4) == "excluded"


def test_save_sidecar_is_atomic_and_loadable(tmp_path):
    path = tmp_path / "x.json"
    doc = progress_io._new_doc({"name": "Tester", "starting_class": None})
    doc["progress"].append({"ids": {"position": "sheet:S|row:1"}, "state": "done"})
    progress_io.save_sidecar(path, doc)

    loaded = progress_io.load_sidecar(path)
    assert loaded is not None
    assert loaded["character"]["name"] == "Tester"
    assert loaded["progress"][0]["state"] == "done"
