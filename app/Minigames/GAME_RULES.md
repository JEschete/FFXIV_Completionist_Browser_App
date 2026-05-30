# Minigame Rules and Requirements

These rules apply to every minigame shipped in this folder.

## Core Requirements

1. Every minigame must be reachable from the Minigames hub page.
2. Every minigame must work on desktop and mobile viewports.
3. Every minigame must support keyboard controls when practical.
4. Every minigame must include a clear in-game controls hint.
5. Every minigame must keep gameplay logic in a dedicated client script under app/Minigames/<GameName>/.
6. Every minigame must persist history by character in data/MinigameData via backend API routes.
7. History records must include a timestamp, a state value, and game-specific metrics.
8. History records must be deletable from the game UI.

## Timing Rules

1. If a game has a timer, the timer starts on the first valid player action.
2. Timers must not start on page load, game load, or puzzle scramble.

## Persistence Rules

1. Use the schema managed by app/game_engine.py.
2. Store runs under games.<minigame_key>.runs.
3. Keep runs sorted newest-first by finished_at (fallback started_at).
4. Enforce retention limits using MINIGAME_MAX_RUNS_PER_GAME.

## UX Rules

1. The Minigames hub card layout should avoid over-stretched single cards on wide screens.
2. Game history tables should display empty-state messaging when no runs exist.
3. Destructive actions (delete run) should require explicit user confirmation.
