# Changelog

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
