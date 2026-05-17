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
- Scrapes authenticated Lodestone data for a character and imports it back into
  the workbook, marking matched rows done (quests, achievements, minions,
  mounts, Triple Triad cards, blue magic, emotes, orchestrion rolls).

## Requirements

- Python 3.10+
- A workbook (`.xlsx`) in the `Spreadsheet/` folder
- For Lodestone import: a signed-in Lodestone session in Edge, Chrome, or
  Firefox on the same machine (cookies are read locally)

Dependencies are listed in `requirements.txt`:

- `fastapi`, `uvicorn`, `jinja2`, `python-multipart`
- `openpyxl` (ingest)
- `httpx`, `beautifulsoup4` (wiki crawler)
- `requests`, `browser-cookie3` (Lodestone probe / authenticated scrape)

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

## Lodestone Probe & Import

The app can scrape a character's authenticated Lodestone pages and replay the
results into the workbook as completed rows. Two pages drive the workflow:

- `GET /lodestone-probe` — saves the character's Lodestone URL, runs an
  authenticated scrape in a background thread, and writes a payload JSON to
  `data/lodestone_probe/`.
- `GET /characters` — upload or select a payload, pick a character, and start
  an import. Matched rows are flipped to `done` (or `100%` for value-style
  rows); unmatched items are written to an HTML / JSON report.

### Workflow

1. Open `/lodestone-probe` and save your Lodestone character URL.
2. Click **Open in new tab** and sign in to Lodestone in your chosen browser
   (Edge / Chrome / Firefox). The cookie source must match.
3. Back on the probe page, pick the cookie source browser, leave **Include
   standard authenticated pages** checked, and click **Run authenticated
   scrape**. A status panel polls `/lodestone-probe/status` until the run
   reports `completed` and a payload path.
4. Go to `/characters`, choose the target character, then either upload the
   payload JSON or select it from the server-side dropdown
   (`data/lodestone_probe/*.json`).
5. Optionally tick **Clear existing character progress before import** to start
   from a blank slate. Click **Start import**.
6. The Import monitor polls `/characters/import-status`. When complete, an
   **Open unmatched items** link surfaces anything the matcher could not place
   so you can review or refile manually.

### Matching notes

- Matching is alias-aware: it handles Roman/Arabic numeral suffixes, optional
  "Card" / "Orchestrion Roll" suffixes, `&` vs `and`, and Lodestone's category
  wrappers (e.g. `Abalathian Sidequests (A Cropper's Duty)`).
- Lodestone exposes built-in/default emotes that are not tracked in the
  workbook — those are silently skipped rather than being reported as
  unmatched.
- An "already done" row is not toggled again; the import summary reports it
  under `rows_skipped_already_done`.

## Run and Health

```powershell
uvicorn app.main:app --reload
```

Useful endpoint:

- `GET /health` -> `{"status":"ok"}`

### Exposing the app on your LAN

By default the app binds to `127.0.0.1`, which only accepts connections from
the host machine. To make it reachable from other devices on the same network
(phones, tablets, other PCs), bind to all interfaces.

**Simplest — pass the host on the command line (no code changes):**

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Then on another device on the same Wi-Fi / LAN, open
`http://<your-pc-lan-ip>:8000` (e.g. `http://192.168.1.42:8000`). On Windows
you can find the LAN IP with `ipconfig` — use the IPv4 address of your active
adapter.

**Alternative — edit `app/main.py`:**

The `__main__` block at the bottom of [app/main.py](app/main.py) hardcodes
the host so that running `python -m app.main` (or pressing the IDE's Run
button) uses a known address:

```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
```

Change `host="127.0.0.1"` to either `"0.0.0.0"` (all interfaces) or your
specific LAN IP (`"192.168.1.42"`) and re-run. Binding to a specific LAN IP
limits exposure to that interface, which is useful when you have multiple
adapters (e.g. a VPN you don't want to publish over).

**Firewall:** the first time you bind to a non-loopback address, Windows
Defender Firewall will prompt to allow Python through — accept it for
**Private** networks only. If you missed the prompt and the page won't load
from another device, allow `python.exe` (or open TCP 8000) under
Windows Security → Firewall & network protection → Allow an app through
firewall.

**Security note:** the app has no authentication. Only do this on networks
you trust, and don't forward the port to the internet.

> Heads up: the bind host should eventually live in an env var / config file
> (e.g. `FFXIV_HOST`) rather than being hardcoded in `app/main.py`. Until
> that's wired up, prefer the `uvicorn --host` flag above so you don't have
> to keep editing source.

## HTTP Surface

Main pages:

- `GET /` dashboard
- `GET /browse/{sheet_name}` menu/content browser
- `GET /chains` chains overview
- `GET /characters` character management + Lodestone import UI
- `GET /lodestone-probe` Lodestone authenticated scrape UI

HTMX/API actions:

- `POST /api/toggle`
- `POST /api/set-value`
- `POST /api/toggle-excluded`
- `POST /api/set-state`
- `POST /api/complete-chain`
- `GET /api/chain/{sheet_name}/{row_index}`
- `GET /api/progress-header`
- `GET /api/search`

Character management:

- `POST /characters/create`
- `POST /characters/select`
- `POST /characters/set-class`
- `POST /characters/delete`

Lodestone scrape (background job):

- `POST /lodestone-probe/save` save Lodestone URL cookie
- `POST /lodestone-probe/run` start authenticated scrape
- `GET /lodestone-probe/status?run_id=...` poll JSON status / log tail

Lodestone import (background job):

- `POST /characters/import-lodestone` upload payload or select server file
- `GET /characters/import-status?run_id=...` poll JSON status / log tail
- `GET /characters/import-unmatched?run_id=...` HTML report of unmatched items
- `GET /characters/import-unmatched.json?run_id=...` same data as JSON

Export:

- `GET /export/current.csv`

## Project Structure

```
app/
  main.py                  FastAPI routes + app lifespan reconcile
  db.py                    data layer, state transitions, rollups, chains
  progress_io.py           sidecar JSON persistence + reconciliation
  lodestone_import.py      Lodestone payload -> workbook row matcher/applier
  templates/
    base.html
    dashboard.html
    menu.html
    sheet.html
    chains.html
    characters.html
    lodestone_probe.html
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

CharacterScraping/
  lodestone_probe.py       authenticated Lodestone scraper used by /lodestone-probe

scripts/
  prep_xlsx_to_sqlite.py   workbook ingest
  crawl_quest_wiki.py      optional wiki crawler / parser / chain graph builder

data/
  ffxiv_tracker.sqlite
  progress/
    <CharacterName>.json
  lodestone_probe/
    <timestamp>_*.json     saved Lodestone payloads
    logs/                  probe run logs
    import_logs/           import run logs
    import_uploads/        payloads uploaded via the Characters page
    unmatched/             unmatched-item reports per import run

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

### Lodestone scrape fails to authenticate

- Confirm you are signed into Lodestone in the same browser you selected as
  the cookie source (Edge / Chrome / Firefox).
- Cookies are read from your installed browser profile via `browser-cookie3`;
  closing the browser is not required, but a recent sign-in is.
- The probe rejects URLs that do not live under `finalfantasyxiv.com/lodestone/`.

### Lodestone import shows many unmatched items

- Open the **Unmatched items** link from the import monitor — each row lists
  the bucket, the aliases that were tried, and a reason
  (`not_found_in_workbook`, `mapped_to_other_bucket`,
  `quest_wrapper_unresolved`, `label_variant_card_suffix`).
- Default emotes that ship with the game are intentionally skipped (they are
  not represented in the workbook) and will not appear in the report.

### UI notes

- The sidebar can be hidden via the "Hide Menu" / "Show Menu" button on the
  topbar. On screens below 880px it becomes a slide-in drawer with a tappable
  backdrop; it defaults to collapsed on mobile so the content area fills the
  viewport.
