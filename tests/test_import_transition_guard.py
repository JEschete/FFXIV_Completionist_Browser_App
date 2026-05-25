"""Import transition report guard behavior."""
from __future__ import annotations

from pathlib import Path

import app.main as main_mod
from app import db, lodestone_import as li


def _seed_run_entry(run_id: str, character_id: int) -> None:
    with main_mod.CHAR_IMPORT_RUNS_LOCK:
        main_mod.CHAR_IMPORT_RUNS[run_id] = {
            "id": run_id,
            "status": "queued",
            "character_id": character_id,
            "logs": [],
        }


def test_initial_import_skips_transition_report(conn, character_id, monkeypatch, tmp_path):
    connection, run_id = conn
    char = db.get_character(connection, character_id)
    assert char is not None

    called = {"reports": 0}

    def fake_create_between_run_report(*args, **kwargs):
        called["reports"] += 1
        return ({"summary": {}}, tmp_path / "should-not-exist.json")

    def fake_import_desktop_completion(
        conn,
        *,
        character_id: int,
        completion_path: Path,
        clear_existing: bool,
        progress,
    ):
        db.set_row_state(conn, character_id, run_id, "Side Stuff", 5, "done")
        progress("fake desktop import applied")
        return li.ImportSummary(
            character_id=character_id,
            character_name=str(char["name"]),
            source_path=str(completion_path),
            run_id=run_id,
            total_candidates=1,
            matched_candidates=1,
            unmatched_candidates=0,
            rows_applied=1,
            rows_skipped_already_done=0,
            unmatched_items=[],
        )

    monkeypatch.setattr(
        main_mod.progress_report,
        "create_between_run_report",
        fake_create_between_run_report,
    )
    monkeypatch.setattr(
        main_mod.lodestone_import,
        "import_desktop_completion",
        fake_import_desktop_completion,
    )

    import_run_id = "test-initial-import"
    _seed_run_entry(import_run_id, character_id)
    source_path = tmp_path / "completion.json"
    source_path.write_text("{}", encoding="utf-8")

    try:
        main_mod._run_character_import_job(
            import_run_id,
            import_type="desktop-app",
            character_id=character_id,
            source_path=source_path,
            clear_existing=False,
            lodestone_level_mode="keep-highest",
        )

        with main_mod.CHAR_IMPORT_RUNS_LOCK:
            run = dict(main_mod.CHAR_IMPORT_RUNS[import_run_id])

        assert run.get("status") == "completed", run
        assert run.get("progress_report_path") is None
        summary = run.get("summary")
        assert isinstance(summary, dict)
        counts = summary.get("existing_progress_before_import")
        assert counts == {"row_overrides": 0, "class_overrides": 0, "total": 0}
        assert called["reports"] == 0
    finally:
        with main_mod.CHAR_IMPORT_RUNS_LOCK:
            main_mod.CHAR_IMPORT_RUNS.pop(import_run_id, None)


def test_existing_progress_still_creates_transition_report(conn, character_id, monkeypatch, tmp_path):
    connection, run_id = conn
    char = db.get_character(connection, character_id)
    assert char is not None

    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")

    report_path = tmp_path / "between.json"
    called = {"reports": 0}

    def fake_create_between_run_report(*args, **kwargs):
        called["reports"] += 1
        return (
            {
                "summary": {
                    "characters_changed": 1,
                    "review_unresolved": 1,
                }
            },
            report_path,
        )

    def fake_import_desktop_completion(
        conn,
        *,
        character_id: int,
        completion_path: Path,
        clear_existing: bool,
        progress,
    ):
        db.set_row_state(conn, character_id, run_id, "Side Stuff", 4, "done")
        progress("fake desktop import applied")
        return li.ImportSummary(
            character_id=character_id,
            character_name=str(char["name"]),
            source_path=str(completion_path),
            run_id=run_id,
            total_candidates=1,
            matched_candidates=1,
            unmatched_candidates=0,
            rows_applied=1,
            rows_skipped_already_done=0,
            unmatched_items=[],
        )

    monkeypatch.setattr(
        main_mod.progress_report,
        "create_between_run_report",
        fake_create_between_run_report,
    )
    monkeypatch.setattr(
        main_mod.lodestone_import,
        "import_desktop_completion",
        fake_import_desktop_completion,
    )

    import_run_id = "test-repeat-import"
    _seed_run_entry(import_run_id, character_id)
    source_path = tmp_path / "completion.json"
    source_path.write_text("{}", encoding="utf-8")

    try:
        main_mod._run_character_import_job(
            import_run_id,
            import_type="desktop-app",
            character_id=character_id,
            source_path=source_path,
            clear_existing=False,
            lodestone_level_mode="keep-highest",
        )

        with main_mod.CHAR_IMPORT_RUNS_LOCK:
            run = dict(main_mod.CHAR_IMPORT_RUNS[import_run_id])

        assert run.get("status") == "completed", run
        assert run.get("progress_report_path") == str(report_path)
        summary = run.get("summary")
        assert isinstance(summary, dict)
        counts = summary.get("existing_progress_before_import")
        assert isinstance(counts, dict)
        assert int(counts.get("total") or 0) > 0
        assert called["reports"] == 1
    finally:
        with main_mod.CHAR_IMPORT_RUNS_LOCK:
            main_mod.CHAR_IMPORT_RUNS.pop(import_run_id, None)


def test_write_import_history_keeps_latest_ten(monkeypatch, tmp_path):
    history_dir = tmp_path / "import_history"
    monkeypatch.setattr(main_mod, "CHAR_IMPORT_HISTORY_DIR", history_dir)

    for idx in range(12):
        run_id = f"run-{idx:02d}"
        out = main_mod._write_import_history(run_id, {"run_id": run_id, "changes": []})
        assert out is not None

    files = [p for p in history_dir.glob("*.json") if p.is_file()]
    stems = {p.stem for p in files}
    assert len(files) == main_mod.MAX_PERSISTED_LOG_FILES_PER_TYPE
    assert "run-00" not in stems
    assert "run-01" not in stems
    assert "run-11" in stems


def test_write_unmatched_report_keeps_latest_ten(monkeypatch, tmp_path):
    unmatched_dir = tmp_path / "unmatched"
    monkeypatch.setattr(main_mod, "CHAR_IMPORT_UNMATCHED_DIR", unmatched_dir)

    summary = li.ImportSummary(
        character_id=1,
        character_name="Test Character",
        source_path="completion.json",
        run_id=1,
        total_candidates=1,
        matched_candidates=0,
        unmatched_candidates=1,
        rows_applied=0,
        rows_skipped_already_done=0,
        unmatched_items=[
            {
                "bucket": "quest",
                "label": "Missing Quest",
                "source_id": "42",
                "source_state": "done",
                "reason": "not_found_in_workbook",
            }
        ],
    )

    for idx in range(12):
        out = main_mod._write_unmatched_report(f"run-{idx:02d}", summary)
        assert out is not None

    files = [p for p in unmatched_dir.glob("*.json") if p.is_file()]
    stems = {p.stem for p in files}
    assert len(files) == main_mod.MAX_PERSISTED_LOG_FILES_PER_TYPE
    assert "run-00" not in stems
    assert "run-01" not in stems
    assert "run-11" in stems
