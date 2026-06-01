from __future__ import annotations

import datetime as dt


def _durability_fill(value: int) -> list[int]:
    return [value for _ in range(7 * 10)]


def _build_run_payload(
    *,
    state: str,
    cause: str,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    duration_ms: int,
    level_reached: int,
    score: int,
    bricks_broken: int,
    balls_lost: int,
    max_combo: int,
    paddle_hits: int,
    wall_bounces: int,
    speed_start_pps: float,
    speed_end_pps: float,
    durability_end: list[int],
) -> dict[str, object]:
    return {
        "state": state,
        "cause": cause,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "board_width": 900,
        "board_height": 520,
        "level_reached": level_reached,
        "score": score,
        "bricks_total": 70,
        "bricks_broken": bricks_broken,
        "balls_lost": balls_lost,
        "max_combo": max_combo,
        "paddle_hits": paddle_hits,
        "wall_bounces": wall_bounces,
        "speed_start_pps": speed_start_pps,
        "speed_end_pps": speed_end_pps,
        "brick_rows": 7,
        "brick_cols": 10,
        "brick_durability_end": durability_end,
        "input_counts": {
            "keyboard": 30,
            "button": 4,
        },
        "powerups_collected": {
            "expand": 2,
            "slow": 1,
            "multiball": 1,
            "life": 0,
        },
    }


def test_breakout_pages_render(client):
    hub = client.get("/minigames")
    assert hub.status_code == 200
    assert "Breakout" in hub.text

    game = client.get("/minigames/breakout")
    assert game.status_code == 200
    assert "data-breakout-app" in game.text
    assert "data-breakout-launch" in game.text
    assert "Press Up to launch" in game.text
    assert "Back to Minigames" in game.text


def test_breakout_history_roundtrip(client, character_id):
    initial = client.get("/api/minigames/breakout/history")
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["character_id"] == character_id
    assert initial_body["history"] == []
    assert initial_body["best_score"] == 0
    assert initial_body["best_level"] == 1
    assert initial_body["best_combo"] == 0
    assert initial_body["longest_duration_ms"] == 0
    assert initial_body["total_runs"] == 0
    assert initial_body["clears"] == 0
    assert initial_body["total_bricks_broken"] == 0

    base = dt.datetime(2026, 1, 8, 9, 0, tzinfo=dt.timezone.utc)
    lost_run = _build_run_payload(
        state="lost",
        cause="drain",
        started_at=base,
        finished_at=base + dt.timedelta(minutes=2),
        duration_ms=120000,
        level_reached=2,
        score=1540,
        bricks_broken=43,
        balls_lost=3,
        max_combo=7,
        paddle_hits=28,
        wall_bounces=89,
        speed_start_pps=340.0,
        speed_end_pps=476.0,
        durability_end=_durability_fill(0),
    )
    won_run = _build_run_payload(
        state="won",
        cause="cleared",
        started_at=base + dt.timedelta(minutes=5),
        finished_at=base + dt.timedelta(minutes=9),
        duration_ms=240000,
        level_reached=5,
        score=4920,
        bricks_broken=70,
        balls_lost=1,
        max_combo=14,
        paddle_hits=61,
        wall_bounces=152,
        speed_start_pps=340.0,
        speed_end_pps=620.0,
        durability_end=_durability_fill(0),
    )

    first = client.post("/api/minigames/breakout/history", json=lost_run)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["total_runs"] == 1
    assert first_body["best_score"] == 1540
    assert first_body["best_level"] == 2
    assert first_body["best_combo"] == 7
    assert first_body["longest_duration_ms"] == 120000
    assert first_body["clears"] == 0
    assert first_body["total_bricks_broken"] == 43

    second = client.post("/api/minigames/breakout/history", json=won_run)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["total_runs"] == 2
    assert second_body["best_score"] == 4920
    assert second_body["best_level"] == 5
    assert second_body["best_combo"] == 14
    assert second_body["longest_duration_ms"] == 240000
    assert second_body["clears"] == 1
    assert second_body["total_bricks_broken"] == 113


def test_breakout_history_delete_run(client):
    base = dt.datetime(2026, 1, 9, 11, 0, tzinfo=dt.timezone.utc)
    run = _build_run_payload(
        state="lost",
        cause="drain",
        started_at=base,
        finished_at=base + dt.timedelta(seconds=50),
        duration_ms=50000,
        level_reached=1,
        score=640,
        bricks_broken=19,
        balls_lost=3,
        max_combo=4,
        paddle_hits=16,
        wall_bounces=40,
        speed_start_pps=340.0,
        speed_end_pps=390.0,
        durability_end=_durability_fill(1),
    )

    saved = client.post("/api/minigames/breakout/history", json=run)
    assert saved.status_code == 200
    run_id = str(saved.json()["history"][0]["id"])

    deleted = client.delete(f"/api/minigames/breakout/history/{run_id}")
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["ok"] is True
    assert deleted_body["total_runs"] == 0


def test_breakout_history_rejects_invalid_payload(client):
    bad_payload = {
        "state": "playing",
        "cause": "drain",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:01+00:00",
        "duration_ms": 1000,
        "board_width": 900,
        "board_height": 520,
        "level_reached": 1,
        "score": 10,
        "bricks_total": 70,
        "bricks_broken": 2,
        "balls_lost": 0,
        "max_combo": 1,
        "paddle_hits": 1,
        "wall_bounces": 2,
        "speed_start_pps": 340.0,
        "speed_end_pps": 360.0,
        "brick_rows": 7,
        "brick_cols": 10,
        "brick_durability_end": _durability_fill(1),
    }

    resp = client.post("/api/minigames/breakout/history", json=bad_payload)
    assert resp.status_code == 400
    assert "Invalid Breakout run payload" in resp.text
