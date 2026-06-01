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


def test_between_run_report_persist_keeps_latest_ten(conn, monkeypatch, tmp_path):
    connection, run_id = conn
    report_root = tmp_path / "logs" / "progress_reports"

    monkeypatch.setattr(progress_report, "_report_root", lambda: report_root)

    baseline = progress_report.build_snapshot(
        connection,
        run_id,
        source="test-baseline",
    )

    for idx in range(12):
        report, report_path = progress_report.create_between_run_report(
            connection,
            run_id,
            reason=f"persist-{idx}",
            baseline=baseline,
            persist=True,
        )
        assert isinstance(report, dict)
        assert report_path is not None

    report_files = [p for p in report_root.glob("between_run_*.json") if p.is_file()]
    assert len(report_files) == progress_report.MAX_PERSISTED_BETWEEN_RUN_REPORTS
    assert (report_root / "latest.json").exists()


def test_between_run_report_integrity_flags_for_spike_and_value_drop(conn, character_id):
    connection, run_id = conn

    db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 50)

    baseline = progress_report.build_snapshot(
        connection,
        run_id,
        source="test-baseline",
    )

    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "excluded")
    db.set_row_state(connection, character_id, run_id, "Story Quests", 4, "excluded")
    db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 5)

    report, _ = progress_report.create_between_run_report(
        connection,
        run_id,
        reason="integrity-check",
        baseline=baseline,
        persist=False,
    )

    integrity = report.get("integrity")
    assert isinstance(integrity, dict)
    alerts = integrity.get("alerts")
    assert isinstance(alerts, list)
    assert alerts

    kinds = {str(alert.get("kind") or "") for alert in alerts if isinstance(alert, dict)}
    assert "excluded_spike" in kinds
    assert "abrupt_value_drop" in kinds

    review_items = report.get("review_items")
    assert isinstance(review_items, list)
    drop_item = next(
        (
            item
            for item in review_items
            if str(item.get("sheet_name") or "") == "Classes-Jobs"
            and int(item.get("row_index") or 0) == 3
        ),
        None,
    )
    assert isinstance(drop_item, dict)
    flags = drop_item.get("integrity_flags")
    assert isinstance(flags, list)
    assert any(str(flag.get("code") or "") == "abrupt_value_drop" for flag in flags if isinstance(flag, dict))


def test_between_run_report_detects_timestamp_anomaly(conn):
    connection, run_id = conn

    baseline = progress_report.build_snapshot(
        connection,
        run_id,
        source="test-baseline",
    )
    baseline_run = baseline.get("run")
    assert isinstance(baseline_run, dict)
    baseline_run["completed_at"] = "2099-01-01T00:00:00"

    report, _ = progress_report.create_between_run_report(
        connection,
        run_id,
        reason="timestamp-check",
        baseline=baseline,
        persist=False,
    )

    integrity = report.get("integrity")
    assert isinstance(integrity, dict)
    alerts = integrity.get("alerts")
    assert isinstance(alerts, list)
    assert any(str(alert.get("kind") or "") == "timestamp_anomaly" for alert in alerts if isinstance(alert, dict))
