# Changelog

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
