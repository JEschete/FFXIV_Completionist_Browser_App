# FFXIV Completion Tracker

A web app for tracking 100% completion of Final Fantasy XIV, built from the
official checklist workbook. FastAPI + HTMX + Alpine, SQLite storage.

## How To Set Up This Project

### 1) Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2) Install dependencies

```powershell
pip install -r requirements.txt
```

### 3) Put the workbook in the Spreadsheet folder

- Place your official checklist workbook (`.xlsx`) in `Spreadsheet/`.
- The importer uses the newest `.xlsx` in that folder by default.

### 4) Build/rebuild the SQLite database

```powershell
python scripts/prep_xlsx_to_sqlite.py
```

Optional overrides:

```powershell
python scripts/prep_xlsx_to_sqlite.py --xlsx .\Spreadsheet\your_workbook.xlsx --db .\data\ffxiv_tracker.sqlite
```

### 5) Run the web app

```powershell
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

### 6) Typical update workflow

1. Replace or add a newer workbook in `Spreadsheet/`.
2. Re-run the importer.
3. Refresh the running app.

## Quick Start

Quick start:

```powershell
pip install -r requirements.txt
python scripts/prep_xlsx_to_sqlite.py
uvicorn app.main:app --reload
```

## Ingest the workbook

```powershell
python scripts/prep_xlsx_to_sqlite.py
```

This reads the newest `*.xlsx` in `Spreadsheet/` by default (or pass `--xlsx <path>`) and
rebuilds `data/ffxiv_tracker.sqlite`. The workbook structure is the source of
truth — the importer captures:

- **Hierarchy** — every content sheet's "Main Page" hyperlink names its parent
  menu, so the full navigation tree is reconstructed.
- **Sections** — purple banner rows split each sheet into sections.
- **Chains** — rows are sequenced within their section, producing linear
  prerequisite edges; "Unlocks" columns become explicit cross-reference edges.
- **State** — the `Complete?` column's `Y` / `N` / `X` map to done / todo /
  excluded. Excluded rows drop out of the completion denominator.

Re-running ingestion preserves existing character progress.

### Reimporting without deleting the database

If you don't delete the old database before reimporting:

1. Your **characters are preserved** with their names and creation dates intact.
2. Your **progress is migrated** to the new sheet structure — but only if row positions haven't changed.

**⚠️ Important caveat:** Progress is tied to `(sheet_name, row_index)` coordinates. If you add, remove, or reorder rows in the workbook, your progress may attach to wrong items or cause constraint errors.

**Safe to reimport if:**
- Only updating completion status values in the spreadsheet (no structural changes).

**Delete the database and reimport if:**
- Adding or removing content sections.
- Reordering rows within sheets.
- Making significant layout changes.

## Run

```powershell
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

## Features

- **Tree navigation** — collapsible sidebar mirroring the workbook's menu
  hierarchy, with per-node progress and breadcrumbs.
- **Category grid** — menu pages show child cards with aggregated progress.
- **Live data tables** — click a row's status to cycle To Do → Done →
  Excluded; progress bars update in place (HTMX, no page reload).
- **Prerequisite chains** — each row's ⛓ opens a drawer showing the steps that
  come before it and what it leads to; "Complete this & all earlier steps"
  cascades the whole chain.
- **Per-character progress** — independent completion state per character.
- **Search** — instant search across every tracked item.
- **CSV export** — `GET /export/current.csv`.

## Layout

```
app/
  main.py            FastAPI routes + HTMX partial endpoints
  db.py              data layer: nav tree, rollups, chains, progress
  templates/         Jinja2 (base, dashboard, menu, sheet, chains, characters)
    partials/        HTMX fragments (row, chain panel, progress header, search)
  static/            styles.css + vendored htmx / alpine
scripts/
  prep_xlsx_to_sqlite.py   workbook -> SQLite ingest
data/
  ffxiv_tracker.sqlite     generated database
```

## Schema

- `ingest_runs` — ingest metadata.
- `sheets` — sheet metadata: title, `is_menu`, `parent_sheet`, data columns.
- `nodes` — one row per workbook row: label, baseline state, row type,
  section, sequence position.
- `edges` — `sequence` (within-section prerequisites) and `unlocks` (explicit
  cross-references).
- `class_overrides` — class-conditional row state overlays derived from workbook formulas.
- `progress_rollup` — cached per-sheet done/excluded/total counters per character.
- `characters`, `character_progress` — per-character state overrides.
