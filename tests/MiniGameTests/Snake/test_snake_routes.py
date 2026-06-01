from __future__ import annotations

import datetime as dt


def _build_run_payload(
    *,
    cause: str,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    duration_ms: int,
    ticks: int,
    steps: int,
    apples: int,
    score: int,
    max_length: int,
    spawn_seed: int,
    spawn_count: int,
    speed_start_tps: float,
    speed_end_tps: float,
    snake_cells: list[int],
    food_cell: int,
) -> dict[str, object]:
    return {
        "state": "lost",
        "cause": cause,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "grid_width": 20,
        "grid_height": 20,
        "ticks": ticks,
        "steps": steps,
        "apples": apples,
        "score": score,
        "max_length": max_length,
        "spawn_seed": spawn_seed,
        "spawn_count": spawn_count,
        "speed_start_tps": speed_start_tps,
        "speed_end_tps": speed_end_tps,
        "final_direction": "right",
        "input_counts": {
            "keyboard": 12,
            "button": 3,
        },
        "snake_cells": snake_cells,
        "food_cell": food_cell,
    }


def test_snake_pages_render(client):
    hub = client.get("/minigames")
    assert hub.status_code == 200
    assert "Snake" in hub.text

    snake = client.get("/minigames/snake")
    assert snake.status_code == 200
    assert "data-snake-app" in snake.text
    assert "data-snake-speed-setting" in snake.text
    assert "Very Hard" in snake.text
    assert "Back to Minigames" in snake.text


def test_snake_history_roundtrip(client, character_id):
    initial = client.get("/api/minigames/snake/history")
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["character_id"] == character_id
    assert initial_body["history"] == []
    assert initial_body["best_score"] == 0
    assert initial_body["best_length"] == 0
    assert initial_body["best_apples"] == 0
    assert initial_body["longest_duration_ms"] == 0
    assert initial_body["total_runs"] == 0
    assert initial_body["wall_hits"] == 0
    assert initial_body["self_hits"] == 0

    base = dt.datetime(2026, 1, 6, 9, 0, tzinfo=dt.timezone.utc)
    wall_run = _build_run_payload(
        cause="wall",
        started_at=base,
        finished_at=base + dt.timedelta(seconds=47),
        duration_ms=47000,
        ticks=376,
        steps=361,
        apples=8,
        score=130,
        max_length=10,
        spawn_seed=123456,
        spawn_count=9,
        speed_start_tps=8.0,
        speed_end_tps=11.2,
        snake_cells=[210, 209, 208, 207, 187, 167, 147, 127, 107, 87],
        food_cell=56,
    )
    self_run = _build_run_payload(
        cause="self",
        started_at=base + dt.timedelta(minutes=2),
        finished_at=base + dt.timedelta(minutes=3),
        duration_ms=60000,
        ticks=512,
        steps=492,
        apples=13,
        score=244,
        max_length=15,
        spawn_seed=789012,
        spawn_count=14,
        speed_start_tps=8.0,
        speed_end_tps=13.6,
        snake_cells=[255, 256, 257, 237, 217, 197, 177, 178, 179, 199, 219, 239, 238, 258, 278],
        food_cell=45,
    )

    first = client.post("/api/minigames/snake/history", json=wall_run)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["total_runs"] == 1
    assert first_body["best_score"] == 130
    assert first_body["best_length"] == 10
    assert first_body["best_apples"] == 8
    assert first_body["longest_duration_ms"] == 47000
    assert first_body["wall_hits"] == 1
    assert first_body["self_hits"] == 0

    second = client.post("/api/minigames/snake/history", json=self_run)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["total_runs"] == 2
    assert second_body["best_score"] == 244
    assert second_body["best_length"] == 15
    assert second_body["best_apples"] == 13
    assert second_body["longest_duration_ms"] == 60000
    assert second_body["wall_hits"] == 1
    assert second_body["self_hits"] == 1


def test_snake_history_delete_run(client):
    base = dt.datetime(2026, 1, 7, 11, 0, tzinfo=dt.timezone.utc)
    run = _build_run_payload(
        cause="wall",
        started_at=base,
        finished_at=base + dt.timedelta(seconds=31),
        duration_ms=31000,
        ticks=250,
        steps=236,
        apples=4,
        score=62,
        max_length=6,
        spawn_seed=456789,
        spawn_count=5,
        speed_start_tps=8.0,
        speed_end_tps=9.6,
        snake_cells=[188, 168, 148, 128, 108, 88],
        food_cell=120,
    )

    saved = client.post("/api/minigames/snake/history", json=run)
    assert saved.status_code == 200
    run_id = str(saved.json()["history"][0]["id"])

    deleted = client.delete(f"/api/minigames/snake/history/{run_id}")
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["ok"] is True
    assert deleted_body["total_runs"] == 0


def test_snake_history_rejects_invalid_payload(client):
    bad_payload = {
        "state": "playing",
        "cause": "wall",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:01+00:00",
        "duration_ms": 1000,
        "grid_width": 20,
        "grid_height": 20,
        "ticks": 1,
        "steps": 1,
        "apples": 0,
        "score": 0,
        "max_length": 2,
        "spawn_seed": 1,
        "spawn_count": 1,
        "speed_start_tps": 8.0,
        "speed_end_tps": 8.0,
        "final_direction": "right",
        "snake_cells": [210, 209],
        "food_cell": 10,
    }

    resp = client.post("/api/minigames/snake/history", json=bad_payload)
    assert resp.status_code == 400
    assert "Invalid Snake run payload" in resp.text
