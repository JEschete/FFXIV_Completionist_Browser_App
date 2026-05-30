from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from pathlib import Path
from typing import Any

MINIGAME_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "MinigameData"
MINIGAME_DATA_LOCK = threading.Lock()
MINIGAME_SLIDE15_KEY = "slide15"
MINIGAME_2048_KEY = "game2048"
MINIGAME_BOMB_FLIP_KEY = "bomb_flip"
MINIGAME_SNAKE_KEY = "snake"
MINIGAME_BREAKOUT_KEY = "breakout"
MINIGAME_MAX_RUNS_PER_GAME = 600
MINIGAME_HISTORY_RESPONSE_LIMIT = 250


def _character_file(character_id: int) -> Path:
    return MINIGAME_DATA_DIR / f"character_{int(character_id)}.json"


def _character_meta(character_row: Any) -> dict[str, Any]:
    return {
        "id": int(character_row["id"]),
        "name": str(character_row["name"] or ""),
        "starting_class": (
            str(character_row["starting_class"]) if character_row["starting_class"] else None
        ),
        "updated_at": dt.datetime.now().isoformat(),
    }


def _empty_doc(character_row: Any) -> dict[str, Any]:
    return {
        "schema": "ffxiv-tracker/minigames-v1",
        "character": _character_meta(character_row),
        "games": {},
        "updated_at": dt.datetime.now().isoformat(),
    }


def load_minigame_doc(character_row: Any) -> tuple[dict[str, Any], Path]:
    path = _character_file(int(character_row["id"]))
    doc: dict[str, Any]
    if not path.exists():
        doc = _empty_doc(character_row)
        return doc, path

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        doc = _empty_doc(character_row)
        return doc, path

    if not isinstance(loaded, dict):
        doc = _empty_doc(character_row)
        return doc, path

    doc = loaded
    doc["schema"] = "ffxiv-tracker/minigames-v1"
    if not isinstance(doc.get("games"), dict):
        doc["games"] = {}

    meta = _character_meta(character_row)
    existing_char = doc.get("character")
    if isinstance(existing_char, dict):
        existing_char.update(
            {
                "id": meta["id"],
                "name": meta["name"],
                "starting_class": meta["starting_class"],
                "updated_at": meta["updated_at"],
            }
        )
        doc["character"] = existing_char
    else:
        doc["character"] = meta

    return doc, path


def save_minigame_doc(path: Path, doc: dict[str, Any]) -> None:
    MINIGAME_DATA_DIR.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = dt.datetime.now().isoformat()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _coerce_slide15_board(raw: Any) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) != 16:
        return None
    try:
        values = [int(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if sorted(values) != list(range(16)):
        return None
    return values


def _coerce_2048_board(raw: Any) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) != 16:
        return None
    values: list[int] = []
    try:
        values = [int(v) for v in raw]
    except (TypeError, ValueError):
        return None
    for value in values:
        if value < 0:
            return None
        if value != 0 and (value & (value - 1)) != 0:
            return None
    return values


def _coerce_bomb_flip_board(raw: Any) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) != 25:
        return None
    try:
        values = [int(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if any(value not in {0, 1, 2, 3} for value in values):
        return None
    return values


def _coerce_bomb_flip_hints(raw: Any) -> list[list[int]] | None:
    if not isinstance(raw, list) or len(raw) != 5:
        return None
    hints: list[list[int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        try:
            points = int(item[0])
            bombs = int(item[1])
        except (TypeError, ValueError):
            return None
        if points < 0 or bombs < 0:
            return None
        hints.append([points, bombs])
    return hints


def _coerce_snake_cells(raw: Any, *, board_cells: int) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) <= 0:
        return None

    cells: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            cell = int(item)
        except (TypeError, ValueError):
            return None
        if cell < 0 or cell >= board_cells or cell in seen:
            return None
        seen.add(cell)
        cells.append(cell)
    return cells


def _coerce_breakout_bricks(raw: Any, *, expected_count: int) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) != expected_count:
        return None
    try:
        values = [int(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if any(value < 0 or value > 9 for value in values):
        return None
    return values


def _coerce_iso_ts(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def normalize_slide15_run(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    state = str(raw.get("state") or "").strip().lower()
    if state not in {"solved", "abandoned"}:
        return None

    started_at = _coerce_iso_ts(raw.get("started_at"))
    finished_at = _coerce_iso_ts(raw.get("finished_at"))
    if started_at is None or finished_at is None:
        return None

    try:
        duration_ms = int(raw.get("duration_ms") or 0)
        actions = int(raw.get("actions") or 0)
        score = int(raw.get("score") or 0)
        aps = float(raw.get("actions_per_second") or 0.0)
    except (TypeError, ValueError):
        return None

    if duration_ms < 0 or actions < 0 or score < 0 or aps < 0:
        return None

    board_start = _coerce_slide15_board(raw.get("board_start"))
    board_end = _coerce_slide15_board(raw.get("board_end"))
    if board_start is None or board_end is None:
        return None

    input_counts_raw = raw.get("input_counts")
    if not isinstance(input_counts_raw, dict):
        input_counts_raw = {}
    try:
        keyboard_count = max(0, int(input_counts_raw.get("keyboard") or 0))
        click_count = max(0, int(input_counts_raw.get("click") or 0))
    except (TypeError, ValueError):
        keyboard_count = 0
        click_count = 0

    try:
        scramble_depth = max(0, int(raw.get("scramble_depth") or 0))
    except (TypeError, ValueError):
        scramble_depth = 0

    return {
        "id": uuid.uuid4().hex,
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "actions": actions,
        "actions_per_second": round(aps, 4),
        "score": score,
        "board_size": 4,
        "scramble_depth": scramble_depth,
        "input_counts": {
            "keyboard": keyboard_count,
            "click": click_count,
        },
        "board_start": board_start,
        "board_end": board_end,
        "saved_at": dt.datetime.now().isoformat(),
    }


def slide15_game_payload(game_doc: dict[str, Any]) -> dict[str, Any]:
    raw_runs = game_doc.get("runs")
    runs = _sorted_runs(raw_runs)
    history = runs[:MINIGAME_HISTORY_RESPONSE_LIMIT]
    solved_scores = [
        int(run.get("score") or 0)
        for run in runs
        if str(run.get("state") or "").lower() == "solved"
    ]
    best_score = max(solved_scores) if solved_scores else 0
    return {
        "history": history,
        "best_score": best_score,
        "total_runs": len(runs),
    }


def normalize_2048_run(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    state = str(raw.get("state") or "").strip().lower()
    if state not in {"won", "lost"}:
        return None

    started_at = _coerce_iso_ts(raw.get("started_at"))
    finished_at = _coerce_iso_ts(raw.get("finished_at"))
    if started_at is None or finished_at is None:
        return None

    try:
        moves = int(raw.get("moves") or 0)
        score = int(raw.get("score") or 0)
        max_tile_input = int(raw.get("max_tile") or 0)
    except (TypeError, ValueError):
        return None
    if moves < 0 or score < 0 or max_tile_input < 0:
        return None

    board_end = _coerce_2048_board(raw.get("board_end"))
    if board_end is None:
        return None
    max_tile = max(board_end) if board_end else 0
    if max_tile_input > 0:
        max_tile = max(max_tile, max_tile_input)

    input_counts_raw = raw.get("input_counts")
    if not isinstance(input_counts_raw, dict):
        input_counts_raw = {}
    try:
        keyboard_count = max(0, int(input_counts_raw.get("keyboard") or 0))
        click_count = max(0, int(input_counts_raw.get("click") or 0))
    except (TypeError, ValueError):
        keyboard_count = 0
        click_count = 0

    return {
        "id": uuid.uuid4().hex,
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "moves": moves,
        "score": score,
        "max_tile": max_tile,
        "board_size": 4,
        "input_counts": {
            "keyboard": keyboard_count,
            "click": click_count,
        },
        "board_end": board_end,
        "saved_at": dt.datetime.now().isoformat(),
    }


def game2048_payload(game_doc: dict[str, Any]) -> dict[str, Any]:
    raw_runs = game_doc.get("runs")
    runs = _sorted_runs(raw_runs)
    history = runs[:MINIGAME_HISTORY_RESPONSE_LIMIT]
    best_score = max((int(run.get("score") or 0) for run in runs), default=0)
    best_tile = max((int(run.get("max_tile") or 0) for run in runs), default=0)
    wins = sum(1 for run in runs if str(run.get("state") or "").lower() == "won")
    return {
        "history": history,
        "best_score": best_score,
        "best_tile": best_tile,
        "total_runs": len(runs),
        "wins": wins,
    }


def normalize_bomb_flip_run(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    state = str(raw.get("state") or "").strip().lower()
    if state not in {"cleared", "bombed"}:
        return None

    started_at = _coerce_iso_ts(raw.get("started_at"))
    finished_at = _coerce_iso_ts(raw.get("finished_at"))
    if started_at is None or finished_at is None:
        return None

    try:
        level_start = int(raw.get("level_start") or 0)
        level_end = int(raw.get("level_end") or 0)
        round_score = int(raw.get("round_score") or 0)
        total_score_after = int(raw.get("total_score_after") or 0)
    except (TypeError, ValueError):
        return None
    if level_start < 1 or level_end < 1 or round_score < 1 or total_score_after < 0:
        return None

    board_revealed = _coerce_bomb_flip_board(raw.get("board_revealed"))
    row_hints = _coerce_bomb_flip_hints(raw.get("row_hints"))
    col_hints = _coerce_bomb_flip_hints(raw.get("col_hints"))
    if board_revealed is None or row_hints is None or col_hints is None:
        return None

    config_raw = raw.get("config")
    if not isinstance(config_raw, dict):
        config_raw = {}
    try:
        twos = max(0, int(config_raw.get("twos") or 0))
        threes = max(0, int(config_raw.get("threes") or 0))
        bombs = max(0, int(config_raw.get("bombs") or 0))
    except (TypeError, ValueError):
        return None
    if twos + threes + bombs > 25:
        return None

    input_counts_raw = raw.get("input_counts")
    if not isinstance(input_counts_raw, dict):
        input_counts_raw = {}
    try:
        left_click = max(0, int(input_counts_raw.get("left_click") or input_counts_raw.get("left") or 0))
        right_click = max(0, int(input_counts_raw.get("right_click") or input_counts_raw.get("right") or 0))
        keyboard = max(0, int(input_counts_raw.get("keyboard") or 0))
    except (TypeError, ValueError):
        left_click = 0
        right_click = 0
        keyboard = 0

    return {
        "id": uuid.uuid4().hex,
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
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
        "row_hints": row_hints,
        "col_hints": col_hints,
        "board_revealed": board_revealed,
        "input_counts": {
            "left_click": left_click,
            "right_click": right_click,
            "keyboard": keyboard,
        },
        "saved_at": dt.datetime.now().isoformat(),
    }


def bomb_flip_payload(game_doc: dict[str, Any]) -> dict[str, Any]:
    raw_runs = game_doc.get("runs")
    runs = _sorted_runs(raw_runs)
    history = runs[:MINIGAME_HISTORY_RESPONSE_LIMIT]
    best_total_score = max((int(run.get("total_score_after") or 0) for run in runs), default=0)
    highest_level = max(
        (
            max(int(run.get("level_start") or 1), int(run.get("level_end") or 1))
            for run in runs
        ),
        default=1,
    )
    clears = sum(1 for run in runs if str(run.get("state") or "").lower() == "cleared")
    last_total_score = int(runs[0].get("total_score_after") or 0) if runs else 0
    last_level = int(runs[0].get("level_end") or 1) if runs else 1
    return {
        "history": history,
        "best_total_score": best_total_score,
        "highest_level": highest_level,
        "last_total_score": last_total_score,
        "last_level": max(1, last_level),
        "total_runs": len(runs),
        "clears": clears,
    }


def normalize_snake_run(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    state = str(raw.get("state") or "").strip().lower()
    if state != "lost":
        return None

    cause = str(raw.get("cause") or "").strip().lower()
    if cause not in {"wall", "self"}:
        return None

    started_at = _coerce_iso_ts(raw.get("started_at"))
    finished_at = _coerce_iso_ts(raw.get("finished_at"))
    if started_at is None or finished_at is None:
        return None

    try:
        grid_width = int(raw.get("grid_width") or 0)
        grid_height = int(raw.get("grid_height") or 0)
        ticks = int(raw.get("ticks") or 0)
        steps = int(raw.get("steps") or 0)
        apples = int(raw.get("apples") or 0)
        score = int(raw.get("score") or 0)
        max_length = int(raw.get("max_length") or 0)
        duration_ms = int(raw.get("duration_ms") or 0)
        spawn_seed = int(raw.get("spawn_seed") or 0)
        spawn_count = int(raw.get("spawn_count") or 0)
        speed_start_tps = float(raw.get("speed_start_tps") or 0.0)
        speed_end_tps = float(raw.get("speed_end_tps") or 0.0)
    except (TypeError, ValueError):
        return None

    if grid_width < 6 or grid_width > 64 or grid_height < 6 or grid_height > 64:
        return None
    if ticks < 0 or steps < 0 or apples < 0 or score < 0 or max_length < 2 or duration_ms < 0:
        return None
    if spawn_seed < 0 or spawn_count < 0:
        return None
    if speed_start_tps <= 0 or speed_end_tps <= 0 or speed_end_tps < speed_start_tps:
        return None

    board_cells = grid_width * grid_height
    snake_cells = _coerce_snake_cells(raw.get("snake_cells"), board_cells=board_cells)
    if snake_cells is None:
        return None
    if len(snake_cells) > board_cells:
        return None
    if max_length < len(snake_cells):
        return None

    try:
        food_cell = int(raw.get("food_cell") if raw.get("food_cell") is not None else -1)
    except (TypeError, ValueError):
        return None
    if food_cell != -1 and (food_cell < 0 or food_cell >= board_cells):
        return None

    final_direction = str(raw.get("final_direction") or "").strip().lower()
    if final_direction not in {"up", "down", "left", "right"}:
        return None

    input_counts_raw = raw.get("input_counts")
    if not isinstance(input_counts_raw, dict):
        input_counts_raw = {}
    try:
        keyboard = max(0, int(input_counts_raw.get("keyboard") or 0))
        button = max(0, int(input_counts_raw.get("button") or input_counts_raw.get("click") or 0))
    except (TypeError, ValueError):
        keyboard = 0
        button = 0

    return {
        "id": uuid.uuid4().hex,
        "state": state,
        "cause": cause,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "grid_width": grid_width,
        "grid_height": grid_height,
        "ticks": ticks,
        "steps": steps,
        "apples": apples,
        "score": score,
        "max_length": max_length,
        "spawn_seed": spawn_seed,
        "spawn_count": spawn_count,
        "speed_start_tps": round(speed_start_tps, 4),
        "speed_end_tps": round(speed_end_tps, 4),
        "final_direction": final_direction,
        "input_counts": {
            "keyboard": keyboard,
            "button": button,
        },
        "snake_cells": snake_cells,
        "food_cell": food_cell,
        "saved_at": dt.datetime.now().isoformat(),
    }


def snake_payload(game_doc: dict[str, Any]) -> dict[str, Any]:
    raw_runs = game_doc.get("runs")
    runs = _sorted_runs(raw_runs)
    history = runs[:MINIGAME_HISTORY_RESPONSE_LIMIT]
    best_score = max((int(run.get("score") or 0) for run in runs), default=0)
    best_length = max((int(run.get("max_length") or 0) for run in runs), default=0)
    best_apples = max((int(run.get("apples") or 0) for run in runs), default=0)
    longest_duration_ms = max((int(run.get("duration_ms") or 0) for run in runs), default=0)
    wall_hits = sum(1 for run in runs if str(run.get("cause") or "").lower() == "wall")
    self_hits = sum(1 for run in runs if str(run.get("cause") or "").lower() == "self")
    return {
        "history": history,
        "best_score": best_score,
        "best_length": best_length,
        "best_apples": best_apples,
        "longest_duration_ms": longest_duration_ms,
        "total_runs": len(runs),
        "wall_hits": wall_hits,
        "self_hits": self_hits,
    }


def normalize_breakout_run(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    state = str(raw.get("state") or "").strip().lower()
    if state not in {"won", "lost"}:
        return None

    cause = str(raw.get("cause") or "").strip().lower()
    if state == "lost" and cause != "drain":
        return None
    if state == "won" and cause not in {"cleared", "drain"}:
        return None

    started_at = _coerce_iso_ts(raw.get("started_at"))
    finished_at = _coerce_iso_ts(raw.get("finished_at"))
    if started_at is None or finished_at is None:
        return None

    try:
        duration_ms = int(raw.get("duration_ms") or 0)
        board_width = int(raw.get("board_width") or 0)
        board_height = int(raw.get("board_height") or 0)
        level_reached = int(raw.get("level_reached") or 0)
        score = int(raw.get("score") or 0)
        bricks_total = int(raw.get("bricks_total") or 0)
        bricks_broken = int(raw.get("bricks_broken") or 0)
        balls_lost = int(raw.get("balls_lost") or 0)
        max_combo = int(raw.get("max_combo") or 0)
        paddle_hits = int(raw.get("paddle_hits") or 0)
        wall_bounces = int(raw.get("wall_bounces") or 0)
        speed_start_pps = float(raw.get("speed_start_pps") or 0.0)
        speed_end_pps = float(raw.get("speed_end_pps") or 0.0)
        brick_rows = int(raw.get("brick_rows") or 0)
        brick_cols = int(raw.get("brick_cols") or 0)
    except (TypeError, ValueError):
        return None

    if duration_ms < 0:
        return None
    if board_width < 320 or board_width > 2400 or board_height < 320 or board_height > 2400:
        return None
    if level_reached < 1 or level_reached > 100:
        return None
    if score < 0 or bricks_total <= 0 or bricks_broken < 0 or bricks_broken > bricks_total:
        return None
    if balls_lost < 0 or max_combo < 0 or paddle_hits < 0 or wall_bounces < 0:
        return None
    if speed_start_pps <= 0 or speed_end_pps <= 0 or speed_end_pps < speed_start_pps:
        return None
    if brick_rows < 1 or brick_rows > 30 or brick_cols < 1 or brick_cols > 30:
        return None

    durability_count = brick_rows * brick_cols
    brick_durability_end = _coerce_breakout_bricks(
        raw.get("brick_durability_end"),
        expected_count=durability_count,
    )
    if brick_durability_end is None:
        return None

    input_counts_raw = raw.get("input_counts")
    if not isinstance(input_counts_raw, dict):
        input_counts_raw = {}
    try:
        keyboard = max(0, int(input_counts_raw.get("keyboard") or 0))
        button = max(0, int(input_counts_raw.get("button") or input_counts_raw.get("click") or 0))
    except (TypeError, ValueError):
        keyboard = 0
        button = 0

    powerups_raw = raw.get("powerups_collected")
    if not isinstance(powerups_raw, dict):
        powerups_raw = {}
    try:
        expand = max(0, int(powerups_raw.get("expand") or 0))
        slow = max(0, int(powerups_raw.get("slow") or 0))
        multiball = max(0, int(powerups_raw.get("multiball") or 0))
        life = max(0, int(powerups_raw.get("life") or 0))
    except (TypeError, ValueError):
        return None

    return {
        "id": uuid.uuid4().hex,
        "state": state,
        "cause": cause,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "board_width": board_width,
        "board_height": board_height,
        "level_reached": level_reached,
        "score": score,
        "bricks_total": bricks_total,
        "bricks_broken": bricks_broken,
        "balls_lost": balls_lost,
        "max_combo": max_combo,
        "paddle_hits": paddle_hits,
        "wall_bounces": wall_bounces,
        "speed_start_pps": round(speed_start_pps, 4),
        "speed_end_pps": round(speed_end_pps, 4),
        "brick_rows": brick_rows,
        "brick_cols": brick_cols,
        "brick_durability_end": brick_durability_end,
        "input_counts": {
            "keyboard": keyboard,
            "button": button,
        },
        "powerups_collected": {
            "expand": expand,
            "slow": slow,
            "multiball": multiball,
            "life": life,
        },
        "saved_at": dt.datetime.now().isoformat(),
    }


def breakout_payload(game_doc: dict[str, Any]) -> dict[str, Any]:
    raw_runs = game_doc.get("runs")
    runs = _sorted_runs(raw_runs)
    history = runs[:MINIGAME_HISTORY_RESPONSE_LIMIT]
    best_score = max((int(run.get("score") or 0) for run in runs), default=0)
    best_level = max((int(run.get("level_reached") or 1) for run in runs), default=1)
    best_combo = max((int(run.get("max_combo") or 0) for run in runs), default=0)
    longest_duration_ms = max((int(run.get("duration_ms") or 0) for run in runs), default=0)
    clears = sum(1 for run in runs if str(run.get("state") or "").lower() == "won")
    total_bricks_broken = sum(int(run.get("bricks_broken") or 0) for run in runs)
    return {
        "history": history,
        "best_score": best_score,
        "best_level": best_level,
        "best_combo": best_combo,
        "longest_duration_ms": longest_duration_ms,
        "total_runs": len(runs),
        "clears": clears,
        "total_bricks_broken": total_bricks_broken,
    }


def _sorted_runs(raw_runs: Any) -> list[dict[str, Any]]:
    runs = [run for run in raw_runs if isinstance(run, dict)] if isinstance(raw_runs, list) else []
    runs.sort(
        key=lambda run: str(run.get("finished_at") or run.get("started_at") or ""),
        reverse=True,
    )
    return runs


def delete_game_run(game_doc: dict[str, Any], run_id: Any) -> bool:
    run_id_value = str(run_id or "").strip()
    if not run_id_value:
        return False

    runs = _sorted_runs(game_doc.get("runs"))
    kept_runs = [
        run
        for run in runs
        if str(run.get("id") or "").strip() != run_id_value
    ]
    if len(kept_runs) == len(runs):
        return False

    game_doc["runs"] = kept_runs
    game_doc["updated_at"] = dt.datetime.now().isoformat()
    return True


def delete_slide15_run(game_doc: dict[str, Any], run_id: Any) -> bool:
    return delete_game_run(game_doc, run_id)
