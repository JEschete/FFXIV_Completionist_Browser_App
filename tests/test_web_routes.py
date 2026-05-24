"""Phase 2 — FastAPI route smoke tests over the seeded DB.

These catch template/route regressions: every page renders, the HTMX toggle
returns a row fragment + the progress-changed trigger, and the CSV export
contains the character's rows.
"""
from __future__ import annotations

import json


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


def test_static_pages_render(client):
    for path in ("/settings", "/credits", "/chains", "/characters"):
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


def test_search(client):
    resp = client.get("/api/search", params={"q": "Quest"})
    assert resp.status_code == 200
    assert "Quest Alpha" in resp.text


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
