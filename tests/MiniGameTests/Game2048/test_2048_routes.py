from __future__ import annotations

import datetime as dt


def _build_run_payload(
    *,
    state: str,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    moves: int,
    score: int,
    max_tile: int,
    board_end: list[int],
) -> dict[str, object]:
    return {
        "state": state,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "moves": moves,
        "score": score,
        "max_tile": max_tile,
        "board_size": 4,
        "input_counts": {
            "keyboard": max(0, moves - 1),
            "click": 1 if moves > 0 else 0,
        },
        "board_end": board_end,
    }


def test_2048_pages_render(client):
    hub = client.get("/minigames")
    assert hub.status_code == 200
    assert "2048" in hub.text

    game = client.get("/minigames/2048")
    assert game.status_code == 200
    assert "data-2048-app" in game.text
    assert "no timer" in game.text.lower()
    assert "Back to Minigames" in game.text


def test_2048_history_roundtrip(client, character_id):
    initial = client.get("/api/minigames/2048/history")
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["character_id"] == character_id
    assert initial_body["history"] == []
    assert initial_body["best_score"] == 0
    assert initial_body["best_tile"] == 0
    assert initial_body["total_runs"] == 0

    base = dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc)
    won_run = _build_run_payload(
        state="won",
        started_at=base,
        finished_at=base + dt.timedelta(minutes=3),
        moves=142,
        score=20480,
        max_tile=2048,
        board_end=[0, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 0, 0, 0, 0],
    )
    lost_run = _build_run_payload(
        state="lost",
        started_at=base + dt.timedelta(minutes=5),
        finished_at=base + dt.timedelta(minutes=9),
        moves=115,
        score=15040,
        max_tile=1024,
        board_end=[2, 4, 2, 4, 4, 8, 16, 32, 8, 16, 32, 64, 128, 256, 512, 1024],
    )

    first = client.post("/api/minigames/2048/history", json=won_run)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["total_runs"] == 1
    assert first_body["best_score"] == 20480
    assert first_body["best_tile"] == 2048
    assert first_body["wins"] == 1

    second = client.post("/api/minigames/2048/history", json=lost_run)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["total_runs"] == 2
    assert second_body["best_score"] == 20480
    assert second_body["best_tile"] == 2048
    assert second_body["wins"] == 1

    persisted = client.get("/api/minigames/2048/history")
    assert persisted.status_code == 200
    persisted_body = persisted.json()
    assert persisted_body["total_runs"] == 2
    assert len(persisted_body["history"]) == 2


def test_2048_history_delete_run(client):
    base = dt.datetime(2026, 1, 3, 9, 0, tzinfo=dt.timezone.utc)
    run = _build_run_payload(
        state="lost",
        started_at=base,
        finished_at=base + dt.timedelta(minutes=1),
        moves=44,
        score=4096,
        max_tile=256,
        board_end=[2, 4, 8, 16, 32, 64, 128, 256, 4, 8, 16, 32, 2, 4, 8, 16],
    )

    saved = client.post("/api/minigames/2048/history", json=run)
    assert saved.status_code == 200
    run_id = str(saved.json()["history"][0]["id"])

    deleted = client.delete(f"/api/minigames/2048/history/{run_id}")
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["ok"] is True
    assert deleted_body["total_runs"] == 0



def test_2048_history_rejects_invalid_payload(client):
    bad_payload = {
        "state": "running",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:01+00:00",
        "moves": 5,
        "score": 500,
        "max_tile": 64,
        "board_end": [1, 2, 3],
    }

    resp = client.post("/api/minigames/2048/history", json=bad_payload)
    assert resp.status_code == 400
    assert "Invalid 2048 run payload" in resp.text
