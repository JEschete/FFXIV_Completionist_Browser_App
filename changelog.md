# Changelog

## Unreleased — UI / Navigation Pass

Date: 2026-05-17

### Sidebar Toggle
- Replaced the small icon-only toggle with a labeled high-contrast button ("Show Menu" / "Hide Menu") with an accent gradient, hover glow, active-press feedback, and focus ring.

### Topbar Navigation
- Moved the "Overview" link out of the breadcrumb row and into the `.quick-links` group alongside Chains / Characters / Lodestone Probe / Export CSV. Active state lights up when the path is `/`.
- Crumbs nav now only renders when there are actual breadcrumbs (no more redundant "Overview ›" prefix on every page).

### Mobile Sidebar
- Sidebar now works on mobile (≤880px) as a slide-in drawer instead of being hidden entirely.
- Default collapsed state is set from `window.innerWidth < 880` on page load.
- Added a tappable backdrop (`.sidebar-backdrop`) that dims the page and closes the drawer on click.
- Sidebar uses `position: fixed` + `transform: translateX(...)` for the slide animation; toggle button stays visible on mobile so the drawer can be reopened.

### Files Touched
- `app/templates/base.html` — Alpine state, quick-links order, conditional crumbs, backdrop element. Stylesheet cache bumped to `?v=11`.
- `app/static/styles.css` — new `.sidebar-toggle` button styling, `.sidebar-backdrop` rules, rewritten `@media (max-width: 880px)` block for the drawer behavior.

---

## Unreleased (Since Last Commit)

Date: 2026-05-17

### Summary
- 7 tracked files modified
- 11 untracked files added
- Diffstat on tracked files: 1027 insertions, 12 deletions

### Modified Files

#### .gitignore
- Expanded ignore patterns for generated/runtime artifacts.
- Added ignore coverage for Lodestone probe outputs and related local data.

#### app/main.py
- Major feature expansion for Lodestone probing and import workflows.
- Added background run orchestration and run status/log handling.
- Added endpoints and integration paths for authenticated Lodestone data ingestion.

#### app/progress_io.py
- Adjusted cache invalidation/locking behavior.
- Includes deadlock-avoidance improvements around cache lock usage.

#### app/static/styles.css
- Added substantial styling for new UI surfaces.
- Includes styles for Lodestone import/probe interactions and status displays.

#### app/templates/base.html
- Small base template updates to support new UI routes/components.

#### app/templates/characters.html
- Added/updated character management UI for Lodestone workflows.
- Includes controls and sections tied to import/probe operations.

#### requirements.txt
- Dependency updates for new scraping/import functionality.
- Added browser-cookie3 and httpx.
- Added requests.

### Added (Untracked) Files

#### CharacterScraping/
- CharacterScraping/lodestone_probe.py
- CharacterScraping/menu_input.txt

#### GameDataReferences/
- GameDataReferences/LandingPAge.html
- GameDataReferences/_seen.txt
- GameDataReferences/_targeted_cats_done.txt
- GameDataReferences/_targeted_titles.txt
- GameDataReferences/categories.json
- GameDataReferences/chains.json
- GameDataReferences/quests.jsonl

#### app/
- app/lodestone_import.py
- app/templates/lodestone_probe.html

### Diffstat (Tracked Files)
- .gitignore: 20 lines changed
- app/main.py: 702 lines changed
- app/progress_io.py: 11 lines changed
- app/static/styles.css: 150 lines changed
- app/templates/base.html: 3 lines changed
- app/templates/characters.html: 151 lines changed
- requirements.txt: 2 lines changed

---

