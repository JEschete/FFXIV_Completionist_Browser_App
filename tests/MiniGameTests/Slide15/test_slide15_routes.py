from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _build_run_payload(
    *,
    state: str,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    duration_ms: int,
    actions: int,
    aps: float,
    score: int,
    board_start: list[int],
    board_end: list[int],
) -> dict[str, object]:
    return {
        "state": state,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "actions": actions,
        "actions_per_second": aps,
        "score": score,
        "board_size": 4,
        "scramble_depth": 180,
        "input_counts": {
            "keyboard": max(0, actions - 1),
            "click": 1 if actions > 0 else 0,
        },
        "board_start": board_start,
        "board_end": board_end,
    }


def test_minigame_pages_render(client):
    hub = client.get("/minigames")
    assert hub.status_code == 200
    assert "Slide 15" in hub.text

    slide15 = client.get("/minigames/slide15")
    assert slide15.status_code == 200
    assert "data-slide15-app" in slide15.text
    assert "data-slide15-restart" in slide15.text
    assert "Back to Minigames" in slide15.text


def test_slide15_history_roundtrip_persists_by_character(client, character_id, ingested_db):
    initial = client.get("/api/minigames/slide15/history")
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["character_id"] == character_id
    assert initial_body["history"] == []
    assert initial_body["best_score"] == 0
    assert initial_body["total_runs"] == 0

    base = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    solved_run = _build_run_payload(
        state="solved",
        started_at=base,
        finished_at=base + dt.timedelta(seconds=22),
        duration_ms=22000,
        actions=31,
        aps=1.409,
        score=4120,
        board_start=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 0, 14, 15],
        board_end=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0],
    )
    abandoned_run = _build_run_payload(
        state="abandoned",
        started_at=base + dt.timedelta(minutes=1),
        finished_at=base + dt.timedelta(minutes=1, seconds=12),
        duration_ms=12000,
        actions=18,
        aps=1.5,
        score=9999,
        board_start=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0, 15],
        board_end=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0, 15],
    )

    first = client.post("/api/minigames/slide15/history", json=solved_run)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["character_id"] == character_id
    assert first_body["total_runs"] == 1
    assert first_body["best_score"] == 4120

    second = client.post("/api/minigames/slide15/history", json=abandoned_run)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["total_runs"] == 2
    # Best score only considers solved runs.
    assert second_body["best_score"] == 4120
    assert second_body["history"][0]["state"] == "abandoned"
    assert second_body["history"][1]["state"] == "solved"

    persisted = client.get("/api/minigames/slide15/history")
    assert persisted.status_code == 200
    persisted_body = persisted.json()
    assert persisted_body["total_runs"] == 2
    assert len(persisted_body["history"]) == 2

    data_file = Path(ingested_db) / "MinigameData" / f"character_{character_id}.json"
    assert data_file.exists()

    doc = json.loads(data_file.read_text(encoding="utf-8"))
    assert doc["character"]["id"] == character_id
    assert "slide15" in doc["games"]
    assert len(doc["games"]["slide15"]["runs"]) == 2


def test_slide15_history_delete_run(client, character_id, ingested_db):
    base = dt.datetime(2026, 2, 1, 13, 0, tzinfo=dt.timezone.utc)
    run_one = _build_run_payload(
        state="solved",
        started_at=base,
        finished_at=base + dt.timedelta(seconds=18),
        duration_ms=18000,
        actions=26,
        aps=1.44,
        score=5000,
        board_start=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0, 13, 14, 15],
        board_end=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0],
    )
    run_two = _build_run_payload(
        state="abandoned",
        started_at=base + dt.timedelta(minutes=1),
        finished_at=base + dt.timedelta(minutes=1, seconds=10),
        duration_ms=10000,
        actions=12,
        aps=1.2,
        score=2100,
        board_start=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0, 15],
        board_end=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0, 15],
    )

    client.post("/api/minigames/slide15/history", json=run_one)
    second = client.post("/api/minigames/slide15/history", json=run_two)
    assert second.status_code == 200
    second_body = second.json()
    delete_id = str(second_body["history"][0]["id"])

    deleted = client.delete(f"/api/minigames/slide15/history/{delete_id}")
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["ok"] is True
    assert deleted_body["character_id"] == character_id
    assert deleted_body["total_runs"] == 1
    assert all(str(row.get("id") or "") != delete_id for row in deleted_body["history"])

    persisted = client.get("/api/minigames/slide15/history")
    assert persisted.status_code == 200
    persisted_body = persisted.json()
    assert persisted_body["total_runs"] == 1
    assert all(str(row.get("id") or "") != delete_id for row in persisted_body["history"])

    data_file = Path(ingested_db) / "MinigameData" / f"character_{character_id}.json"
    doc = json.loads(data_file.read_text(encoding="utf-8"))
    saved_runs = doc["games"]["slide15"]["runs"]
    assert len(saved_runs) == 1
    assert all(str(run.get("id") or "") != delete_id for run in saved_runs)


def test_slide15_history_delete_unknown_run_returns_404(client):
    resp = client.delete("/api/minigames/slide15/history/not-a-real-run-id")
    assert resp.status_code == 404
    assert "Run not found" in resp.text


def test_slide15_history_rejects_invalid_payload(client):
    bad_payload = {
        "state": "in-progress",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:01+00:00",
        "duration_ms": 1000,
        "actions": 1,
        "actions_per_second": 1.0,
        "score": 1,
        "board_start": [1, 2, 3],
        "board_end": [1, 2, 3],
    }

    resp = client.post("/api/minigames/slide15/history", json=bad_payload)
    assert resp.status_code == 400
    assert "Invalid Slide 15 run payload" in resp.text
