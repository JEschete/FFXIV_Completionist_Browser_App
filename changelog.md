# Changelog

## v1.0.8 — 2026-05-24

### Between-Run Progress Reports
- Added persistent progress baseline/report support under `data/logs/progress_reports` with timestamped reports plus `latest.json` and `progress_baseline.json`.
- Added automatic between-run transition reports during startup/ingest reconciliation and import transitions.
- Added a new `Progress Reports` page with per-character filtering, unresolved review counts, and advanced added/removed/orphaned detail views.
- Added report review resolution actions (`done`, `excluded`, `todo`) in the web UI, including apply-and-save behavior for report-backed row changes.
- Added an on-demand report API endpoint at `/api/progress/between-run-report` for generating and optionally persisting snapshot diffs.

### Import and Data Hardening
- Added import transition guard behavior: initial imports with no prior explicit progress now skip transition report generation and reset the baseline.
- Added clear-existing import handling that resets the baseline instead of producing noisy transition reports.
- Import run summaries now include existing explicit progress counts, optional history artifacts, unmatched report paths, and progress report paths.
- Lodestone JSON import now syncs `Classes-Jobs` level rows from `class_job` payload data with merge modes (`keep-highest` default, `overwrite` optional).
- Hardened progress write paths with immediate transaction acquisition and rollback-safe sidecar write-through for row state updates and override clears.
- Deleting a character now also removes rollup rows and cleans up/invalidate related sidecar files.

### Workbook Ingest and Structure Hardening
- Added pre-ingest baseline snapshot capture when required tables are present, with graceful fallback when snapshot capture is unavailable.
- Schema rebuild now restores latest per-row progress deterministically via windowed ranking, with compatibility fallback for older/partial schemas.
- Improved submenu parent resolution to avoid ambiguous shared cross-links during hierarchy inference.
- Added explicit content parent override support (including `Fish Guide` under `Fishing Logs`).

### UI and Navigation Data Improvements
- Added row-type aware export rows to support richer report comparisons and review-item actions.
- Added trackable rollup helpers for virtual content group aggregation, including label-prefix grouping support for `Hunting Logs`.

### CI and Test Coverage
- Added GitHub Actions CI workflow in `.github/workflows/ci.yml` with:
	- `ruff check` linting (plus advisory format check),
	- `pytest` + coverage reporting,
	- OS/Python matrix coverage (`ubuntu-latest` and `windows-latest`; Python `3.10` and `3.13`).
- Expanded regression coverage across between-run reporting, import transition guards, DB rollups/helpers, ingest helpers, scraper/parser paths, and web routes.

### Included Commits Since v1.0.7
- `9414245` Data hardening
- `3e50d77` Initial CI/CD pipeline and data hardening
- `06ef1a5` Diff report implemented
- `d61d120` Merge pull request #33 from `JEschete/progressLoss`

## v1.0.7 — 2026-05-23

### Desktop App Import
- Merged inline Desktop App custom metadata (`overall.custom` + top-level `custom`) into source indexing so custom entries no longer fail as `id_not_in_source_index` when metadata exists in `completion.json`.
- Added positional mapping support for numeric buckets in `Classes-Jobs` and `Desynthesis`, including nested bucket paths such as `character/character/classes-jobs`.
- Numeric completion values now preserve their real values instead of being clamped to 100 during decode.
- Value-row imports now apply through cap-aware value writes, preserving row caps (for example desynthesis rows up to 770) instead of forcing 100.

### Workbook Ingest and Diagnostics
- Added optional ingest memory checkpoints (`--mem-log`) to report process RSS throughout workbook processing.
- Added Server Manager GUI support for ingest memory logging with a persistent checkbox in Settings.
- Ingest runs launched from the GUI now write per-run logs to `data/logs/ingest_*.log`.
- Improved Windows memory reporting reliability with explicit process-memory probing and a `tasklist` fallback.
- Reduced peak ingest memory on very wide sheets by replacing broad row scans with targeted row-index reads and bounded menu-column scans.
- Improved workbook memory lifecycle handling with explicit close/finally cleanup to release resources sooner.

### UI and Rendering
- Fixed settings page cap-input alignment behavior and bumped stylesheet cache version so CSS updates apply immediately.
- Fixed template rendering crash when non-value rows do not include `value_cap`.
- Crafting divider rows such as `91-100`, `81-90`, and `(See Shared Craft Log)` now ingest as section rows (not empty checkbox rows), preventing blank dash rows in crafting logs.

## v1.0.6 — 2026-05-22

### Desktop App Import
- Added full progress import support from the Desktop App `completion.json` flow.
- Import runtime now scales with completion size and account history depth; larger profiles can take noticeably longer, so users should wait for the monitor to finish.
- Import monitor messaging now clearly reports applied rows, already-set rows, unmatched candidate counts, skipped known-untracked entries, and unmatched report output location.
- Typical successful completion output looks like:

```text
[23:04:43] Desktop import complete: applied=30813, already_set=0, unmatched_candidates=126
[23:04:43] Unmatched reasons: not_found_in_workbook=126
[23:04:43] Skipped 15 known-untracked desktop entries
[23:04:43] Saved unmatched report to D:\Dev\FFXIVTracker\data\lodestone_probe\unmatched\9a09b011d5c148369a9fe3dc1278b0a9.json
[23:04:43] Desktop import job completed
[done] applied=30813, matched=29073/29214, unmatched=126
```

### Themes, Settings, and UI Polish
- Added a dedicated Settings page with persistent theme controls.
- Added theme and scheme selection (`default`, `dark`, `light`) sourced from `app/themes/*.json`.
- Added server-side theme catalog loading and validation with graceful fallback behavior.
- Added first-paint theme-aware background/text injection to reduce wrong-theme flashes on navigation.
- Added shared chrome tokens (`sidebar-top`, `sidebar-bottom`, `chrome-top`, `chrome-bottom`) and applied them to the sidebar plus top-right controls so those regions now follow active theme values.
- Updated theme ordering in the Settings dropdown to: FF by numeric order, then Pokemon by generation acronym order (RBY, GSC, RSE, DPPL), then non-FF themes.
- Added Vagrant Story palette tuning for both light and dark schemes based on visual references.
- Updated topbar quick-link order: `Characters` now appears before `Chains`, and `Credits` now appears to the right of `Settings`.
- Fixed submenu parenting during ingest using menu hyperlink targets so `Companion Rank` and `Companion Skills` stay under `Char. Menu - Companion` (and future ingests keep this structure).
- Added a Lodestone language reminder in the probe workflow and docs to switch Lodestone to English before scraping/importing.

### Issue Updates
- #17 When downloading and launching a new installer from the GUI, the running GUI now closes before the installer starts to avoid locked-file and update-flow conflicts. Status: fixed. Test status: needs test.
- #19 JSON import from file picker could fail while dropdown selection incorrectly took precedence in some flows. Import source precedence is now explicit and file-picker handling is corrected. Status: fixed. Test status: tested.
- #12 Mobile sidebar flash on page load has been addressed with first-paint and hydration timing fixes to prevent visible panel flicker. Status: fixed. Test status: tested.
- #18 Checklist sections are now collapsible with per-section toggles and persisted open/closed state for faster navigation on large sheets. Status: implemented. Test status: tested.
- #21 Column sorting is now available on sheet tables, including toggle behavior for directional sorting and reset to original order. Status: implemented. Test status: tested.
- #9 Bulk edit actions are now available on sheet tables for faster section-wide state updates (Done, To Do, Excluded), including guardrails for large updates. Status: implemented. Test status: tested.
- #10 Undo and redo for progress changes are now available with multi-step history support for row edits, bulk actions, and import-driven changes. Status: implemented. Test status: tested.

## v1.0.0 — 2026-05-17

Initial public release.

### Core Features
- FastAPI + HTMX + Alpine web app serving a local completionist tracker
- Workbook ingest: reads the official `.xlsx` checklist into SQLite for fast queries
- Per-character progress tracking with JSON sidecar files that survive workbook rebuilds and row movement
- Chain-aware progression with prerequisite and unlock links
- Class overlay support for class-specific row exclusions
- CSV export of the active character's effective completion state

### Lodestone Import
- Authenticated Lodestone scraper reads quests, achievements, minions, mounts, Triple Triad cards, Blue Magic, emotes, and orchestrion rolls from a signed-in browser session (Edge, Chrome, or Firefox)
- Tiered identity matching (name → alias → position fallback) for resilient row matching across workbook rebuilds
- Unmatched item reports generated per import run for manual review

### UI
- Sidebar tree mirroring the workbook hierarchy with aggregated progress per section
- Mobile sidebar works as a slide-in drawer on screens ≤880px with a tappable backdrop to dismiss
- Topbar quick-links: Overview, Chains, Characters, Lodestone Probe, Export CSV
- Sidebar toggle button with high-contrast label and focus ring for accessibility
- Breadcrumb row only renders when actual breadcrumbs exist
- Added character portrait in top left when doing a lodestone import. 

### Launcher
- `launch.cmd` bootstraps a `.venv`, installs dependencies, and hands off to an interactive menu
- Menu options: status check, workbook ingest, open data folder, backup to zip, clean probe artifacts, reinstall deps, set LAN bind IP, open Discord, start server

### Fixes
- Quest rows with a slash in the name (e.g. "School of Hard Nocks") now ingest and match correctly
- Progress sidecar cache invalidation and deadlock-avoidance improvements
- Character reimport no longer duplicates progress entries
