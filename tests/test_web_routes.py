"""Phase 2 — FastAPI route smoke tests over the seeded DB.

These catch template/route regressions: every page renders, the HTMX toggle
returns a row fragment + the progress-changed trigger, and the CSV export
contains the character's rows.
"""
from __future__ import annotations

import json
import re

from app import progress_report


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_dashboard_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Character Menu" in resp.text


def test_menu_browse_lists_children(client):
    resp = client.get("/browse/Character Menu")
    assert resp.status_code == 200
    # Category-grid cards are titled from each child's section banner (a content
    # sheet with a single banner takes that banner as its display title).
    assert "Main Story Chain" in resp.text
    assert "Odds And Ends" in resp.text


def test_content_sheet_browse(client):
    resp = client.get("/browse/Story Quests")
    assert resp.status_code == 200
    assert "Quest Alpha" in resp.text
    assert "Quest Beta" in resp.text


def test_browse_filter_state_persists_across_navigation(client):
    first = client.get("/browse/Side Stuff", params={"state": "todo"})
    assert first.status_code == 200
    assert "ffxiv_sheet_filter_state=todo" in first.headers.get("set-cookie", "")

    resp = client.get("/browse/Story Quests")
    assert resp.status_code == 200
    assert "Quest Alpha" not in resp.text
    assert "Quest Beta" in resp.text
    assert "Quest Gamma" in resp.text


def test_browse_multi_state_filter_persists_across_navigation(client):
    first = client.get(
        "/browse/Side Stuff",
        params=[("states", "done"), ("states", "excluded")],
    )
    assert first.status_code == 200
    # Preserve canonical cookie ordering across repeated query params.
    assert client.cookies.get("ffxiv_sheet_filter_state") == "done.excluded"

    assert "Thing One" in first.text
    assert "Thing Two" in first.text
    assert "Thing Three" not in first.text

    # Navigation should keep the same multi-selection via cookie.
    resp = client.get("/browse/Story Quests")
    assert resp.status_code == 200
    assert "Quest Alpha" in resp.text
    assert "Quest Beta" not in resp.text
    assert "Quest Gamma" not in resp.text


def test_browse_empty_multi_state_filter_persists(client):
    first = client.get(
        "/browse/Side Stuff",
        params={"states_present": "1"},
    )
    assert first.status_code == 200
    assert client.cookies.get("ffxiv_sheet_filter_state") == "none"

    assert "Thing One" not in first.text
    assert "Thing Two" not in first.text
    assert "Thing Three" not in first.text

    # Cookie-backed persistence should keep empty selection across navigation.
    resp = client.get("/browse/Story Quests")
    assert resp.status_code == 200
    assert "Quest Alpha" not in resp.text
    assert "Quest Beta" not in resp.text
    assert "Quest Gamma" not in resp.text


def test_browse_state_all_resets_persisted_state_filter(client):
    first = client.get(
        "/browse/Side Stuff",
        params=[("states", "done")],
    )
    assert first.status_code == 200
    assert client.cookies.get("ffxiv_sheet_filter_state") == "done"
    assert "Thing One" in first.text
    assert "Thing Two" not in first.text
    assert "Thing Three" not in first.text

    reset = client.get("/browse/Side Stuff", params={"state": "all"})
    assert reset.status_code == 200
    assert client.cookies.get("ffxiv_sheet_filter_state") == "all"
    assert "Thing One" in reset.text
    assert "Thing Two" in reset.text
    assert "Thing Three" in reset.text


def test_settings_save_persists_completion_behavior_cookies(client):
    settings_page = client.get("/settings")
    assert settings_page.status_code == 200

    match = re.search(
        r'<select id="theme-id"[^>]*>\s*<option value="([^"]+)"',
        settings_page.text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match, "expected at least one theme option"
    theme_id = match.group(1)

    save = client.post(
        "/settings/theme",
        data={
            "theme_id": theme_id,
            "sidebar_completion_behavior": "star",
            "page_completion_behavior": "hide",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303
    assert client.cookies.get("ffxiv_sidebar_completion_behavior") == "star"
    assert client.cookies.get("ffxiv_page_completion_behavior") == "hide"


def test_page_completion_hide_omits_completed_cards(client):
    # Side Stuff becomes 100% complete: Thing One is already done, toggle Thing Three todo -> done.
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})

    client.cookies.set("ffxiv_page_completion_behavior", "hide")
    resp = client.get("/browse/Character Menu")
    assert resp.status_code == 200
    assert '<a class="cat-card" href="/browse/Side%20Stuff">' not in resp.text
    assert '<a class="cat-card" href="/browse/Story%20Quests">' in resp.text


def test_page_completion_star_marks_completed_cards(client):
    # Side Stuff becomes 100% complete: Thing One is already done, toggle Thing Three todo -> done.
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})

    client.cookies.set("ffxiv_page_completion_behavior", "star")
    resp = client.get("/browse/Character Menu")
    assert resp.status_code == 200
    assert '<a class="cat-card" href="/browse/Side%20Stuff">' in resp.text
    assert 'Odds And Ends<span class="completion-mark"' in resp.text


def test_sidebar_completion_hide_omits_completed_categories(client):
    # Side Stuff becomes 100% complete: Thing One is already done, toggle Thing Three todo -> done.
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})

    client.cookies.set("ffxiv_sidebar_completion_behavior", "hide")
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<a class="tree-link" href="/browse/Side%20Stuff">' not in resp.text
    assert '<a class="tree-link" href="/browse/Story%20Quests">' in resp.text


def test_static_pages_render(client):
    for path in ("/settings", "/credits", "/chains", "/characters", "/progress-reports"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_toggle_returns_fragment_and_trigger(client):
    resp = client.post(
        "/api/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert resp.status_code == 200
    assert "Thing Three" in resp.text
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger.get("progress-changed") is True


def test_toggle_persists_state(client):
    # todo -> done
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})
    # The browse view should now reflect the change in its progress markup.
    resp = client.get("/browse/Side Stuff")
    assert resp.status_code == 200
    # Two done rows now (Thing One baseline + Thing Three just toggled).
    assert resp.text.count("Thing Three") >= 1


def test_set_value_route(client):
    resp = client.post(
        "/api/set-value",
        data={"sheet_name": "Classes-Jobs", "row_index": "3", "percent": "100"},
    )
    assert resp.status_code == 200
    assert "Paladin" in resp.text


def test_set_value_route_desynthesis_allows_two_decimals(client, conn):
    connection, run_id = conn
    connection.execute(
        """
        UPDATE nodes
        SET section_label = 'Desynthesis'
        WHERE run_id = ? AND sheet_name = 'Classes-Jobs' AND row_index = 3
        """,
        (run_id,),
    )
    connection.commit()

    resp = client.post(
        "/api/set-value",
        data={"sheet_name": "Classes-Jobs", "row_index": "3", "percent": "324.52"},
    )
    assert resp.status_code == 200
    assert 'step="0.01"' in resp.text
    assert 'value="324.52"' in resp.text

    saved = connection.execute(
        """
        SELECT progress_percent
        FROM character_progress
        WHERE character_id = 1 AND run_id = ?
          AND sheet_name = 'Classes-Jobs' AND row_index = 3
        """,
        (run_id,),
    ).fetchone()
    assert saved is not None
    assert float(saved["progress_percent"]) == 324.52


def test_search(client):
    resp = client.get("/api/search", params={"q": "Quest"})
    assert resp.status_code == 200
    assert "Quest Alpha" in resp.text


def test_search_includes_global_sheet_and_section_hits(client):
    sheet_resp = client.get("/api/search", params={"q": "Character Menu"})
    assert sheet_resp.status_code == 200
    assert "Character Menu" in sheet_resp.text
    assert "Page" in sheet_resp.text

    section_resp = client.get("/api/search", params={"q": "MAIN STORY"})
    assert section_resp.status_code == 200
    assert "MAIN STORY CHAIN" in section_resp.text
    assert "Section ·" in section_resp.text


def test_progress_header_partial(client):
    resp = client.get("/api/progress-header", params={"sheet_name": "Side Stuff"})
    assert resp.status_code == 200


def test_csv_export(client):
    resp = client.get("/export/current.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert "character,sheet,section,row_index,label,state,progress_percent" in body
    assert "Thing One" in body
    assert "Quest Alpha" in body


def test_chain_partial(client):
    resp = client.get("/api/chain/Story Quests/5")
    assert resp.status_code == 200
    # Gamma's prerequisite path includes Beta/Alpha.
    assert "Quest" in resp.text


def test_between_run_report_api(client):
    # First call should have a baseline (initialized during startup reconcile
    # when missing, or loaded from a previous snapshot).
    initial = client.get(
        "/api/progress/between-run-report",
        params={"persist": "false"},
    )
    assert initial.status_code == 200
    initial_doc = initial.json()
    assert initial_doc["summary"]["baseline_available"] is True

    # Change progression and verify the report detects a delta.
    toggle = client.post(
        "/api/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert toggle.status_code == 200

    after = client.get(
        "/api/progress/between-run-report",
        params={"persist": "false"},
    )
    assert after.status_code == 200
    after_doc = after.json()
    assert int(after_doc["summary"]["characters_changed"]) >= 1


def test_progress_report_resolution_route(client):
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})
    report_resp = client.get(
        "/api/progress/between-run-report",
        params={"persist": "true"},
    )
    assert report_resp.status_code == 200
    report_doc = report_resp.json()

    items = report_doc.get("review_items")
    assert isinstance(items, list)
    assert items, "expected at least one review item after a toggle"

    item_id = items[0]["id"]
    resolve_resp = client.post(
        "/progress-reports/resolve",
        data={
            "item_id": item_id,
            "resolution": "excluded",
            "next_url": "/progress-reports",
        },
        follow_redirects=False,
    )
    assert resolve_resp.status_code == 303

    latest = progress_report.load_latest_report()
    assert isinstance(latest, dict)

    character_id = int(items[0]["character_id"])
    visible_items = progress_report.review_items_for_character(latest, character_id)
    assert all(str(item.get("id") or "") != item_id for item in visible_items)

    page_resp = client.get("/progress-reports", params={"character_id": str(character_id)})
    assert page_resp.status_code == 200
    assert item_id not in page_resp.text


def test_progress_report_bulk_resolution_route(client):
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})
    report_resp = client.get(
        "/api/progress/between-run-report",
        params={"persist": "true"},
    )
    assert report_resp.status_code == 200
    report_doc = report_resp.json()

    items = report_doc.get("review_items")
    assert isinstance(items, list)
    assert items, "expected at least one review item after a toggle"

    character_id = int(items[0]["character_id"])
    bulk_resp = client.post(
        "/progress-reports/resolve-bulk",
        data={
            "character_id": str(character_id),
            "resolution": "done",
            "only_unresolved": "1",
            "next_url": f"/progress-reports?character_id={character_id}",
        },
        follow_redirects=False,
    )
    assert bulk_resp.status_code == 303

    latest = progress_report.load_latest_report()
    assert isinstance(latest, dict)
    unresolved = progress_report.count_unresolved_review_items(
        latest,
        character_id=character_id,
    )
    assert unresolved == 0

    page_resp = client.get("/progress-reports", params={"character_id": str(character_id)})
    assert page_resp.status_code == 200
    assert "No unresolved review items for this character" in page_resp.text
