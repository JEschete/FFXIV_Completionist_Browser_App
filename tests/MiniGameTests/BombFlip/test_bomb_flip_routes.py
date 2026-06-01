from __future__ import annotations

import datetime as dt


def _sample_hints() -> list[list[int]]:
    return [
        [8, 1],
        [7, 2],
        [9, 1],
        [6, 2],
        [8, 1],
    ]


def _build_run_payload(
    *,
    state: str,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    level_start: int,
    level_end: int,
    round_score: int,
    total_score_after: int,
    board_revealed: list[int],
    twos: int,
    threes: int,
    bombs: int,
) -> dict[str, object]:
    return {
        "state": state,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "level_start": level_start,
        "level_end": level_end,
        "round_score": round_score,
        "total_score_after": total_score_after,
        "board_size": 5,
        "config": {
            "twos": twos,
            "threes": threes,
            "bombs": bombs,
        },
        "row_hints": _sample_hints(),
        "col_hints": _sample_hints(),
        "board_revealed": board_revealed,
        "input_counts": {
            "left_click": 12,
            "right_click": 3,
            "keyboard": 0,
        },
    }


def test_bomb_flip_pages_render(client):
    hub = client.get("/minigames")
    assert hub.status_code == 200
    assert "Bomb Flip" in hub.text

    game = client.get("/minigames/bomb-flip")
    assert game.status_code == 200
    assert "data-bflip-app" in game.text
    assert "Back to Minigames" in game.text


def test_bomb_flip_history_roundtrip(client, character_id):
    initial = client.get("/api/minigames/bomb-flip/history")
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["character_id"] == character_id
    assert initial_body["history"] == []
    assert initial_body["best_total_score"] == 0
    assert initial_body["highest_level"] == 1
    assert initial_body["last_total_score"] == 0
    assert initial_body["last_level"] == 1
    assert initial_body["total_runs"] == 0
    assert initial_body["clears"] == 0

    base = dt.datetime(2026, 1, 4, 9, 0, tzinfo=dt.timezone.utc)
    cleared_run = _build_run_payload(
        state="cleared",
        started_at=base,
        finished_at=base + dt.timedelta(minutes=2),
        level_start=3,
        level_end=4,
        round_score=36,
        total_score_after=322,
        board_revealed=[
            1, 1, 2, 0, 3,
            1, 2, 1, 1, 0,
            3, 1, 1, 2, 1,
            0, 1, 2, 1, 1,
            1, 1, 0, 1, 2,
        ],
        twos=6,
        threes=3,
        bombs=6,
    )
    bombed_run = _build_run_payload(
        state="bombed",
        started_at=base + dt.timedelta(minutes=5),
        finished_at=base + dt.timedelta(minutes=6),
        level_start=4,
        level_end=3,
        round_score=4,
        total_score_after=322,
        board_revealed=[
            0, 1, 1, 1, 2,
            1, 2, 1, 3, 1,
            1, 0, 1, 2, 1,
            1, 1, 2, 1, 0,
            3, 1, 1, 1, 0,
        ],
        twos=5,
        threes=2,
        bombs=7,
    )

    first = client.post("/api/minigames/bomb-flip/history", json=cleared_run)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["total_runs"] == 1
    assert first_body["clears"] == 1
    assert first_body["best_total_score"] == 322
    assert first_body["highest_level"] == 4

    second = client.post("/api/minigames/bomb-flip/history", json=bombed_run)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["total_runs"] == 2
    assert second_body["clears"] == 1
    assert second_body["best_total_score"] == 322
    assert second_body["highest_level"] == 4
    assert second_body["last_total_score"] == 322
    assert second_body["last_level"] == 3


def test_bomb_flip_history_delete_run(client):
    base = dt.datetime(2026, 1, 5, 10, 0, tzinfo=dt.timezone.utc)
    run = _build_run_payload(
        state="bombed",
        started_at=base,
        finished_at=base + dt.timedelta(minutes=1),
        level_start=2,
        level_end=1,
        round_score=1,
        total_score_after=100,
        board_revealed=[
            0, 1, 1, 1, 2,
            1, 2, 1, 3, 1,
            1, 0, 1, 2, 1,
            1, 1, 2, 1, 0,
            3, 1, 1, 1, 0,
        ],
        twos=4,
        threes=2,
        bombs=8,
    )

    saved = client.post("/api/minigames/bomb-flip/history", json=run)
    assert saved.status_code == 200
    run_id = str(saved.json()["history"][0]["id"])

    deleted = client.delete(f"/api/minigames/bomb-flip/history/{run_id}")
    assert deleted.status_code == 200
    deleted_body = deleted.json()
    assert deleted_body["ok"] is True
    assert deleted_body["total_runs"] == 0


def test_bomb_flip_history_rejects_invalid_payload(client):
    bad_payload = {
        "state": "playing",
        "started_at": "2026-01-01T12:00:00+00:00",
        "finished_at": "2026-01-01T12:00:01+00:00",
        "level_start": 2,
        "level_end": 2,
        "round_score": 2,
        "total_score_after": 4,
        "config": {"twos": 2, "threes": 1, "bombs": 7},
        "row_hints": [[1, 1]],
        "col_hints": [[1, 1]],
        "board_revealed": [0, 1, 2],
    }

    resp = client.post("/api/minigames/bomb-flip/history", json=bad_payload)
    assert resp.status_code == 400
    assert "Invalid Bomb Flip run payload" in resp.text
