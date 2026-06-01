"""Phase 1 — db.py state machine, rollup deltas, chains, class overlay.

The cached rollup (progress_rollup) is the riskiest code: it's maintained by a
+/- delta on every write and must never drift from a full recount. Several
tests here assert exactly that by comparing the cached path against the
lock-safe live recount.
"""
from __future__ import annotations

from app import db


def _live(connection, run_id, character_id, starting_class=None):
    return db._sheet_rollups_live(connection, run_id, character_id, starting_class)


def test_baseline_rollup(conn, character_id):
    connection, run_id = conn
    rolls = db.sheet_rollups(connection, run_id, character_id)
    side = rolls["Side Stuff"]
    assert side == {"done": 1, "excluded": 1, "total": 3, "countable": 2}
    assert db.pct(side) == 50.0


def test_next_state_cycle_constant():
    assert db.NEXT_STATE == {"todo": "done", "done": "excluded", "excluded": "todo"}


def test_toggle_cycles_through_states(conn, character_id):
    connection, run_id = conn
    # Thing Three starts todo (non-chain row -> no cascade).
    s1, changed = db.toggle_row(connection, character_id, run_id, "Side Stuff", 5)
    assert s1 == "done" and changed == [5]
    s2, _ = db.toggle_row(connection, character_id, run_id, "Side Stuff", 5)
    assert s2 == "excluded"
    s3, _ = db.toggle_row(connection, character_id, run_id, "Side Stuff", 5)
    assert s3 == "todo"


def test_rollup_delta_matches_recount_after_toggles(conn, character_id):
    connection, run_id = conn
    # Seed the cached rollup, then drive several transitions.
    db.sheet_rollups(connection, run_id, character_id)
    for _ in range(2):
        db.toggle_row(connection, character_id, run_id, "Side Stuff", 5)
        db.toggle_row(connection, character_id, run_id, "Side Stuff", 4)
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 3, "todo")

    cached = db.sheet_rollups(connection, run_id, character_id)["Side Stuff"]
    live = _live(connection, run_id, character_id)["Side Stuff"]
    assert cached == live, "cached rollup drifted from full recount"


def test_total_is_invariant_under_state_change(conn, character_id):
    connection, run_id = conn
    db.sheet_rollups(connection, run_id, character_id)
    before = db.sheet_rollups(connection, run_id, character_id)["Side Stuff"]["total"]
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 3, "excluded")
    after = db.sheet_rollups(connection, run_id, character_id)["Side Stuff"]["total"]
    assert before == after == 3


def test_excluded_leaves_denominator(conn, character_id):
    connection, run_id = conn
    # Exclude the only undone row -> countable drops, done unchanged, pct = 100.
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "excluded")
    roll = db.sheet_rollups(connection, run_id, character_id)["Side Stuff"]
    assert roll["excluded"] == 2 and roll["countable"] == 1 and roll["done"] == 1
    assert db.pct(roll) == 100.0


def test_chain_completion_cascades_backward(conn, character_id):
    connection, run_id = conn
    # Quest Gamma (row 5) done -> Beta (4) and Alpha (3, already done) complete.
    new_state, changed = db.toggle_row(connection, character_id, run_id, "Story Quests", 5)
    assert new_state == "done"
    assert set(changed) >= {4, 5}
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 4) == "done"


def test_chain_revert_cascades_forward(conn, character_id):
    connection, run_id = conn
    # Complete the whole chain first.
    db.toggle_row(connection, character_id, run_id, "Story Quests", 5)
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 5) == "done"
    # Revert Beta (done -> excluded) should knock its successor Gamma back to todo.
    new_state, changed = db.toggle_row(connection, character_id, run_id, "Story Quests", 4)
    assert new_state == "excluded"
    assert db.effective_state(connection, character_id, run_id, "Story Quests", 5) == "todo"


def test_value_row_state_derived_from_cap(conn, character_id):
    connection, run_id = conn
    # Paladin (row 3), default cap 100.
    assert db.value_row_cap(connection, run_id, "Classes-Jobs", 3) == 100.0
    assert db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 90) == "todo"
    assert db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 100) == "done"


def test_value_row_toggle_excluded_preserves_percent(conn, character_id):
    connection, run_id = conn
    db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 100)
    assert db.toggle_excluded(connection, character_id, run_id, "Classes-Jobs", 3) == "excluded"
    # Un-excluding restores the saved level -> back to done (100 >= cap).
    assert db.toggle_excluded(connection, character_id, run_id, "Classes-Jobs", 3) == "done"


def test_value_rows_contribute_weighted_levels(conn, character_id):
    connection, run_id = conn
    roll = db.sheet_rollups(connection, run_id, character_id)["Classes-Jobs"]
    assert roll == {"done": 190, "excluded": 0, "total": 200, "countable": 200}
    assert db.pct(roll) == 95.0


def test_value_cap_override_invalidates_cached_rollup(conn, character_id):
    connection, run_id = conn
    cap_row = db.classes_jobs_cap_rows(connection, run_id)[0]
    override_key = cap_row["cap_key"]

    try:
        db.save_value_cap_overrides({override_key: 80})
        db.clear_progress_rollups(connection)

        roll = db.sheet_rollups(connection, run_id, character_id)["Classes-Jobs"]
        assert roll == {"done": 180, "excluded": 0, "total": 180, "countable": 180}
        assert db.pct(roll) == 100.0
    finally:
        db.save_value_cap_overrides({})
        db.clear_progress_rollups(connection)


def test_class_overlay_path(conn, character_id):
    connection, run_id = conn
    # Inject a class-specific override for Thing Three (row 5) and check the
    # COALESCE(p.state, co.state, baseline) path resolves it only for that class.
    connection.execute(
        "INSERT INTO class_overrides (run_id, starting_class, sheet_name, row_index, state) "
        "VALUES (?, 'GLADIATOR', 'Side Stuff', 5, 'done')",
        (run_id,),
    )
    connection.commit()

    assert db.effective_state(connection, character_id, run_id, "Side Stuff", 5) == "todo"
    assert db.effective_state(
        connection, character_id, run_id, "Side Stuff", 5, "GLADIATOR"
    ) == "done"

    # And the rollup honors the overlay when the character carries the class.
    db.set_character_class(connection, character_id, "GLADIATOR")
    roll = db.sheet_rollups(connection, run_id, character_id, "GLADIATOR")["Side Stuff"]
    assert roll["done"] == 2  # Thing One (baseline) + Thing Three (class overlay)


def test_character_progress_overrides_class_overlay(conn, character_id):
    connection, run_id = conn
    connection.execute(
        "INSERT INTO class_overrides (run_id, starting_class, sheet_name, row_index, state) "
        "VALUES (?, 'GLADIATOR', 'Side Stuff', 5, 'done')",
        (run_id,),
    )
    connection.commit()
    # An explicit character override wins over the class overlay.
    db.set_row_state(
        connection, character_id, run_id, "Side Stuff", 5, "excluded",
        starting_class="GLADIATOR",
    )
    assert db.effective_state(
        connection, character_id, run_id, "Side Stuff", 5, "GLADIATOR"
    ) == "excluded"


def test_class_excluded_overlay_takes_priority_over_explicit_state(conn, character_id):
    connection, run_id = conn
    connection.execute(
        "INSERT INTO class_overrides (run_id, starting_class, sheet_name, row_index, state) "
        "VALUES (?, 'GLADIATOR', 'Side Stuff', 5, 'excluded')",
        (run_id,),
    )
    connection.commit()

    db.set_row_state(
        connection, character_id, run_id, "Side Stuff", 5, "done",
        starting_class="GLADIATOR",
    )

    assert db.effective_state(
        connection, character_id, run_id, "Side Stuff", 5, "GLADIATOR"
    ) == "excluded"
    roll = db.sheet_rollups(connection, run_id, character_id, "GLADIATOR")["Side Stuff"]
    assert roll == {"done": 1, "excluded": 2, "total": 3, "countable": 1}
