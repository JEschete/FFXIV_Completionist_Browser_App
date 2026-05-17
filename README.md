# FFXIV Completion Tracker

A FastAPI + HTMX + Alpine web app for tracking Final Fantasy XIV completion from
the official checklist workbook.

The workbook is ingested into SQLite for fast reads, and each character's
progress is also persisted to JSON sidecar files so progress can survive workbook
rebuilds and row movement.

## What This Project Does

- Mirrors the workbook hierarchy as a sidebar tree with aggregated progress.
- Renders menu sheets as grouped card grids.
- Renders content sheets as live-updating tables.
- Supports chain-aware progression with prerequisite and unlock links.
- Tracks independent progress per character.
- Applies optional starting-class overlays for class-specific exclusions.
- Exports the active character's current effective state as CSV.

## Requirements

- Python 3.10+
- A workbook (`.xlsx`) in the `Spreadsheet/` folder

Dependencies are listed in `requirements.txt`:

- `fastapi`, `uvicorn`, `jinja2`, `python-multipart`
- `openpyxl` (ingest)
- `httpx`, `beautifulsoup4` (wiki crawler)

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Build database from the newest Spreadsheet/*.xlsx
python scripts/prep_xlsx_to_sqlite.py

# Run app
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## Core Workflow

1. Put or update your workbook in `Spreadsheet/`.
2. Run `python scripts/prep_xlsx_to_sqlite.py`.
3. Start or refresh the app.
4. Use `Characters` to manage character profiles and optional starting class.

## Workbook Ingest Deep Dive

Ingest script: `scripts/prep_xlsx_to_sqlite.py`

### Input selection

- Default source: newest `.xlsx` in `Spreadsheet/`
- Override source: `--xlsx <path>`
- Override DB path: `--db <path>`

### What gets extracted

- Parent/child sheet relationships:
  content sheets resolve parent menu via the sheet's `Main Page` hyperlink.
- Section boundaries:
  purple banner rows (`CC66FF`) become section markers.
- Row baseline state:
  column A values map to `done`/`todo`/`excluded`.
- Row type:
  checkbox rows vs value rows (for level/progress style sheets).
- Chain edges:
  sequence edges are created only for true chain sheets/sections.
- Unlock edges:
  unlock/require columns create explicit cross-row links when resolvable.
- Menu section grouping:
  child sheets are mapped to menu-column sections (used for grouped menu cards
  and virtual nav nodes).
- Stable row fingerprint:
  each row stores a normalized hash used by progress reconciliation.

### Rebuild behavior

The ingest rebuilds schema tables on each run, then:

- preserves existing `characters`
- migrates previous run `character_progress` rows to the new run where possible
- recomputes `class_overrides` from workbook formulas

## Progress Model (Important)

Module: `app/progress_io.py`

Progress source of truth is **JSON sidecars** in `data/progress/`.

- On startup, app lifespan calls reconcile logic:
  sidecars are replayed into DB `character_progress` for the latest ingest run.
- On each state change, DB is updated and sidecar is updated atomically.
- If a character has DB progress but no sidecar yet, a one-time sidecar
  bootstrap is created.

### Tiered identity for resilient matching

Each sidecar entry stores four identity tiers (strongest to weakest):

1. `sheet + section + label`
2. `sheet + label`
3. `sheet + row-content hash`
4. `sheet + row index`

On reconcile, matching tries tiers in order. Unresolved entries are kept and
flagged as `orphan`, so they can auto-recover if rows reappear later.

By default, `sheet + row` fallback is disabled for safety (to avoid attaching
progress to the wrong row after workbook reordering). You can opt into
aggressive recovery by setting:

```powershell
$env:FFXIV_PROGRESS_ALLOW_POSITION_FALLBACK = "1"
```

## Chain Behavior

Module: `app/db.py`

- Regular toggle cycle: `todo -> done -> excluded -> todo`
- For chain rows:
  - `todo -> done` cascades backward (completes prerequisites)
  - changing a `done` chain step away from done cascades forward
    (reverts done successors to todo)
- Chain drawer (`⛓`) shows prerequisites, unlocks, blocked status, and supports
  "complete this and all earlier steps".

## Starting Class Overlays

- Character starting class is optional and set in `Characters` page.
- When set, effective row state becomes:
  `character override -> class override -> workbook baseline`.
- Changing class clears cached rollups and reseeds them on next read.

## Run and Health

```powershell
uvicorn app.main:app --reload
```

Useful endpoint:

- `GET /health` -> `{"status":"ok"}`

## HTTP Surface

Main pages:

- `GET /` dashboard
- `GET /browse/{sheet_name}` menu/content browser
- `GET /chains` chains overview
- `GET /characters` character management

HTMX/API actions:

- `POST /api/toggle`
- `POST /api/set-value`
- `POST /api/toggle-excluded`
- `POST /api/set-state`
- `POST /api/complete-chain`
- `GET /api/chain/{sheet_name}/{row_index}`
- `GET /api/progress-header`
- `GET /api/search`

Export:

- `GET /export/current.csv`

## Project Structure

```
app/
  main.py                  FastAPI routes + app lifespan reconcile
  db.py                    data layer, state transitions, rollups, chains
  progress_io.py           sidecar JSON persistence + reconciliation
  templates/
    base.html
    dashboard.html
    menu.html
    sheet.html
    chains.html
    characters.html
    partials/
      row.html
      chain_panel.html
      progress_header.html
      search_results.html
  static/
    styles.css
    vendor/
      htmx.min.js
      alpine.min.js
      alpine-collapse.min.js

scripts/
  prep_xlsx_to_sqlite.py   workbook ingest
  crawl_quest_wiki.py      optional wiki crawler / parser / chain graph builder

data/
  ffxiv_tracker.sqlite
  progress/
    <CharacterName>.json

GameDataReferences/
  quests.jsonl
  chains.json
  categories.json
  cache/
```

## Database Schema (High Level)

- `ingest_runs`: ingest metadata per run
- `sheets`: sheet metadata, parent linkage, menu section grouping
- `nodes`: normalized rows with baseline state and stable hash
- `edges`: `sequence` and `unlocks` relationships
- `class_overrides`: class-dependent state overrides from formulas
- `characters`: character identities
- `character_progress`: per-character row overrides for current run
- `progress_rollup`: cached per-sheet counts (done/excluded/total)

## Optional: Wiki Crawler Tooling

Script: `scripts/crawl_quest_wiki.py`

Capabilities:

- crawl full wiki (`--mode allpages`) or BFS from seed (`--mode bfs`)
- cache HTML pages under `GameDataReferences/cache/`
- parse quest-like pages into `GameDataReferences/quests.jsonl`
- resolve quest links into `GameDataReferences/chains.json`
- interactive menu mode on bare invocation (`python scripts/crawl_quest_wiki.py`)
- category discovery and targeted crawl control via `categories.json`

Examples:

```powershell
# interactive menu
python scripts/crawl_quest_wiki.py

# full-site crawl mode
python scripts/crawl_quest_wiki.py --mode allpages --delay 1.2

# parse existing cache only
python scripts/crawl_quest_wiki.py --parse-only

# rebuild graph only
python scripts/crawl_quest_wiki.py --build-graph-only
```

## Troubleshooting

### "No ingest run found" (HTTP 503)

Run ingest at least once:

```powershell
python scripts/prep_xlsx_to_sqlite.py
```

### Workbook not found

- Ensure `Spreadsheet/` exists in project root.
- Ensure at least one `.xlsx` file is present.
- Or pass `--xlsx <path>` explicitly.

### Character progress appears missing after rebuild

- Check `data/progress/` sidecars exist.
- On app start, sidecars are reconciled into DB for latest run.
- If rows changed heavily, some entries may be marked orphan until a future
  workbook version reintroduces matching identities.
- For one-off aggressive recovery attempts, you can temporarily enable row
  fallback with `FFXIV_PROGRESS_ALLOW_POSITION_FALLBACK=1` before starting the
  app.
