"""Phase 1 — ingest correctness against the synthetic workbook.

These lock in the structural contract the rest of the app reads back out:
sheet parentage, node/edge counts, baseline states, value-sheet detection and
the per-row content hash.
"""
from __future__ import annotations


def _sheets(conn, run_id):
    return {r["sheet_name"]: r for r in conn.execute(
        "SELECT * FROM sheets WHERE run_id = ?", (run_id,)
    )}


def test_ingest_run_recorded(conn):
    connection, run_id = conn
    row = connection.execute(
        "SELECT sheet_count, row_count, completed_at FROM ingest_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row["sheet_count"] == 4          # menu + 3 content sheets
    assert row["row_count"] == 8            # 3 + 3 + 2 trackable rows
    assert row["completed_at"]             # ingest stamps completion


def test_sheet_parentage_and_kind(conn):
    connection, run_id = conn
    sheets = _sheets(connection, run_id)

    assert sheets["Character Menu"]["is_menu"] == 1
    assert sheets["Character Menu"]["parent_sheet"] is None

    for child in ("Story Quests", "Side Stuff", "Classes-Jobs"):
        assert sheets[child]["is_menu"] == 0
        assert sheets[child]["parent_sheet"] == "Character Menu"


def test_value_sheet_detection(conn):
    connection, run_id = conn
    sheets = _sheets(connection, run_id)
    # Classes-Jobs matches VALUE_SHEET_PATTERNS -> picks a numeric value column.
    assert sheets["Classes-Jobs"]["value_key"] == "current_level"
    assert sheets["Classes-Jobs"]["label_key"] == "job"
    # Non-value sheets never pick a value column.
    assert sheets["Story Quests"]["value_key"] is None


def test_baseline_states_from_markers(conn):
    connection, run_id = conn
    rows = {
        (r["sheet_name"], r["label"]): r["baseline_state"]
        for r in connection.execute(
            "SELECT sheet_name, label, baseline_state FROM nodes "
            "WHERE run_id = ? AND row_type != 'section'",
            (run_id,),
        )
    }
    assert rows[("Story Quests", "Quest Alpha")] == "done"      # Y
    assert rows[("Story Quests", "Quest Beta")] == "todo"       # N
    assert rows[("Side Stuff", "Thing One")] == "done"          # Y
    assert rows[("Side Stuff", "Thing Two")] == "excluded"      # X
    assert rows[("Side Stuff", "Thing Three")] == "todo"        # N


def test_row_types(conn):
    connection, run_id = conn
    types = {
        (r["sheet_name"], r["row_index"]): r["row_type"]
        for r in connection.execute(
            "SELECT sheet_name, row_index, row_type FROM nodes WHERE run_id = ?",
            (run_id,),
        )
    }
    assert types[("Classes-Jobs", 3)] == "value"      # Paladin
    assert types[("Story Quests", 3)] == "checkbox"   # Quest Alpha
    assert types[("Story Quests", 2)] == "section"    # banner


def test_chain_edges_only_in_chain_sections(conn):
    connection, run_id = conn
    story_edges = connection.execute(
        "SELECT source_row_index, target_row_index FROM edges "
        "WHERE run_id = ? AND sheet_name = 'Story Quests' AND edge_type = 'sequence' "
        "ORDER BY source_row_index",
        (run_id,),
    ).fetchall()
    # Three consecutive checkboxes in a chain section -> two sequence links.
    assert [(e["source_row_index"], e["target_row_index"]) for e in story_edges] == [(3, 4), (4, 5)]

    # The non-chain sheet produces no sequence edges.
    side_count = connection.execute(
        "SELECT COUNT(*) AS c FROM edges WHERE run_id = ? AND sheet_name = 'Side Stuff'",
        (run_id,),
    ).fetchone()["c"]
    assert side_count == 0


def test_section_nodes_have_no_hash_but_rows_do(conn):
    connection, run_id = conn
    for r in connection.execute(
        "SELECT row_type, stable_hash FROM nodes WHERE run_id = ?", (run_id,)
    ):
        if r["row_type"] == "section":
            assert r["stable_hash"] is None
        else:
            assert r["stable_hash"] and len(r["stable_hash"]) == 12
