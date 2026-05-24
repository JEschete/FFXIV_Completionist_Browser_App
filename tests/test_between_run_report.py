"""Progress between-run/ingest report behavior."""
from __future__ import annotations

from app import db, progress_report


def test_between_run_report_detects_progress_delta(conn, character_id):
    connection, run_id = conn

    baseline = progress_report.build_snapshot(
        connection,
        run_id,
        source="test-baseline",
    )

    # Create a user-visible progression change after the baseline capture.
    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")

    report, _ = progress_report.create_between_run_report(
        connection,
        run_id,
        reason="test",
        baseline=baseline,
        persist=False,
    )

    summary = report.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("baseline_available") is True
    assert int(summary.get("characters_changed") or 0) >= 1

    rows = report.get("characters")
    assert isinstance(rows, list)
    adventurer = next((r for r in rows if str(r.get("name") or "") == "Adventurer"), None)
    assert isinstance(adventurer, dict)
    assert adventurer.get("changed") is True

    delta = adventurer.get("delta")
    assert isinstance(delta, dict)
    assert int(delta.get("done") or 0) >= 1
