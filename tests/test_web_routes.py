"""Phase 2 — FastAPI route smoke tests over the seeded DB.

These catch template/route regressions: every page renders, the HTMX toggle
returns a row fragment + the progress-changed trigger, and the CSV export
contains the character's rows.
"""
from __future__ import annotations

import json
import re
import sqlite3

from app import db, progress_io, progress_report


def _seed_second_ingest_run(connection: sqlite3.Connection, run_id: int) -> int:
    src = connection.execute(
        """
        SELECT source_file, sheet_count, row_count
        FROM ingest_runs
        WHERE id = ?
        """,
        (int(run_id),),
    ).fetchone()
    assert src is not None

    ts = "2026-06-10T01:00:00"
    new_run_id = connection.execute(
        """
        INSERT INTO ingest_runs (source_file, started_at, completed_at, sheet_count, row_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(src["source_file"] or "synthetic-updated.xlsx"),
            ts,
            ts,
            int(src["sheet_count"] or 0),
            int(src["row_count"] or 0),
        ),
    ).lastrowid
    assert new_run_id is not None

    connection.execute(
        """
        INSERT INTO sheets (
            run_id, sheet_index, sheet_name, title, is_menu, is_readonly,
            parent_sheet, parent_menu_section, data_columns_json,
            label_key, value_key, total_rows
        )
        SELECT ?, sheet_index, sheet_name, title, is_menu, is_readonly,
               parent_sheet, parent_menu_section, data_columns_json,
               label_key, value_key, total_rows
        FROM sheets
        WHERE run_id = ?
        """,
        (int(new_run_id), int(run_id)),
    )
    connection.execute(
        """
        INSERT INTO nodes (
            run_id, sheet_name, row_index, label, baseline_state,
            row_type, section_label, seq, row_json, stable_hash
        )
        SELECT ?, sheet_name, row_index, label, baseline_state,
               row_type, section_label, seq, row_json, stable_hash
        FROM nodes
        WHERE run_id = ?
        """,
        (int(new_run_id), int(run_id)),
    )
    connection.execute(
        """
        INSERT INTO nodes (
            run_id, sheet_name, row_index, label, baseline_state,
            row_type, section_label, seq, row_json, stable_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(new_run_id),
            "Side Stuff",
            999,
            "Thing Four",
            "todo",
            "checkbox",
            "ODDS AND ENDS",
            999,
            json.dumps({"label": "Thing Four", "row": 999}),
            "aa11bb22cc33",
        ),
    )
    connection.execute(
        "UPDATE ingest_runs SET row_count = row_count + 1 WHERE id = ?",
        (int(new_run_id),),
    )
    connection.execute(
        """
        UPDATE sheets
        SET total_rows = total_rows + 1
        WHERE run_id = ? AND sheet_name = ?
        """,
        (int(new_run_id), "Side Stuff"),
    )
    connection.commit()
    return int(new_run_id)


def _write_whats_new_fallback_snapshot(
    *,
    missing_sheet_name: str,
    missing_row_index: int,
    previous_run_id: int = 77,
    source_file: str = "Old Checklist.xlsx",
) -> None:
    connection = db.get_connection()
    try:
        run_id = db.latest_run_id(connection)
        assert run_id is not None
        rows = connection.execute(
            """
            SELECT n.sheet_name, n.row_index, n.row_type, n.section_label,
                   n.label, n.row_json, n.stable_hash, s.title AS sheet_title
            FROM nodes n
            JOIN sheets s
              ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
            WHERE n.run_id = ?
              AND n.row_type IN ('checkbox', 'value')
            ORDER BY n.sheet_name, n.row_index
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()

    previous_rows = [
        dict(row)
        for row in rows
        if not (
            str(row["sheet_name"] or "") == missing_sheet_name
            and int(row["row_index"] or 0) == int(missing_row_index)
        )
    ]

    db.WHATS_NEW_PREVIOUS_INGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    db.WHATS_NEW_PREVIOUS_INGEST_PATH.write_text(
        json.dumps(
            {
                "schema_version": "ffxiv-tracker/whats-new-baseline/v1",
                "run": {
                    "id": int(previous_run_id),
                    "source_file": source_file,
                    "started_at": "2026-05-31T10:00:00",
                    "completed_at": "2026-05-31T10:01:00",
                    "sheet_count": 3,
                    "row_count": len(previous_rows),
                },
                "rows": previous_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_dashboard_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Character Menu" in resp.text


def test_dashboard_renders_activity_feed_and_heatmap_before_chains(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Recent activity" in resp.text
    assert "activity-scroll" in resp.text
    assert "Contribution heatmap" in resp.text
    assert "Current streak" in resp.text
    assert "Longest streak" in resp.text
    assert "Today" in resp.text
    assert "This Week" in resp.text
    assert "Older" in resp.text

    recent_pos = resp.text.find("Recent activity")
    heatmap_pos = resp.text.find("Contribution heatmap")
    chains_pos = resp.text.find("Chains in progress")
    assert recent_pos != -1 and heatmap_pos != -1
    if chains_pos != -1:
        assert recent_pos < chains_pos
        assert heatmap_pos < chains_pos


def test_dashboard_activity_feed_lists_touched_rows(client):
    toggle = client.post(
        "/api/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert toggle.status_code == 200

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Recent activity" in resp.text
    assert "Thing Three" in resp.text


def test_dashboard_activity_feed_uses_descriptive_name_for_numeric_label(client):
    connection = db.get_connection()
    try:
        run_id = db.latest_run_id(connection)
        assert run_id is not None
        connection.execute(
            """
            UPDATE nodes
            SET label = ?, row_json = ?
            WHERE run_id = ? AND sheet_name = ? AND row_index = ?
            """,
            (
                "50",
                json.dumps({"fate": "Mint Condition"}),
                run_id,
                "Side Stuff",
                5,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    toggle = client.post(
        "/api/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert toggle.status_code == 200

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Mint Condition" in resp.text


def test_dashboard_activity_ignores_non_done_actions(client):
    first = client.post(
        "/api/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/set-state",
        data={"sheet_name": "Side Stuff", "row_index": "5", "state": "todo"},
    )
    assert second.status_code == 200

    resp = client.get("/")
    assert resp.status_code == 200
    assert "0 done actions" in resp.text
    assert "Thing Three" not in resp.text


def test_dashboard_heatmap_tiles_render(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "heatmap-day level-" in resp.text


def test_share_cards_page_renders(client):
    resp = client.get("/share-cards")
    assert resp.status_code == 200
    assert "Shareable progress cards" in resp.text
    assert "Download PNG" in resp.text
    assert "share-theme" in resp.text


def test_share_cards_page_shows_weekly_improvement_after_done_toggle(client):
    toggle = client.post(
        "/api/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert toggle.status_code == 200

    resp = client.get("/share-cards")
    assert resp.status_code == 200
    assert "+1 done" in resp.text


def test_whats_new_page_renders(client):
    resp = client.get("/whats-new")
    assert resp.status_code == 200
    assert "What's New in Latest Ingest" in resp.text
    assert "No previous ingest run yet" in resp.text


def test_whats_new_page_lists_new_items_from_latest_run(client):
    connection = db.get_connection()
    try:
        run_id = db.latest_run_id(connection)
        assert run_id is not None
        _seed_second_ingest_run(connection, run_id)
    finally:
        connection.close()

    resp = client.get("/whats-new")
    assert resp.status_code == 200
    assert "New items by sheet" in resp.text
    assert "Added rows" in resp.text
    assert "Thing Four" in resp.text


def test_whats_new_page_uses_pre_ingest_snapshot_fallback(client):
    _write_whats_new_fallback_snapshot(
        missing_sheet_name="Side Stuff",
        missing_row_index=5,
    )

    resp = client.get("/whats-new")
    assert resp.status_code == 200
    assert "No previous ingest run yet" not in resp.text
    assert "Thing Three" in resp.text
    assert "#77" in resp.text


def test_dashboard_shows_whats_new_toast_when_unreviewed(client):
    _write_whats_new_fallback_snapshot(
        missing_sheet_name="Side Stuff",
        missing_row_index=5,
    )

    resp = client.get("/")
    assert resp.status_code == 200
    assert "What's New update available" in resp.text
    assert "Review What's New" in resp.text


def test_dashboard_whats_new_toast_clears_after_review(client):
    _write_whats_new_fallback_snapshot(
        missing_sheet_name="Side Stuff",
        missing_row_index=5,
    )

    first_dashboard = client.get("/")
    assert first_dashboard.status_code == 200
    assert "What's New update available" in first_dashboard.text

    review_page = client.get("/whats-new")
    assert review_page.status_code == 200
    assert "Thing Three" in review_page.text

    second_dashboard = client.get("/")
    assert second_dashboard.status_code == 200
    assert "What's New update available" not in second_dashboard.text

    # Reviewing should clear only the toast state, not the What's New data itself.
    still_there = client.get("/whats-new")
    assert still_there.status_code == 200
    assert "Thing Three" in still_there.text


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


def test_dashboard_chains_in_progress_excludes_completed_chains(client, monkeypatch):
    import app.main as main_mod

    def fake_chain_sheets_overview(*_args, **_kwargs):
        return [
            {
                "sheet_name": "Synthetic Complete Chain",
                "title": "Synthetic Complete Chain",
                "links": 8,
                "roll": {"done": 10, "countable": 10, "excluded": 0, "total": 10},
                "pct": 100.0,
            },
            {
                "sheet_name": "Synthetic Active Chain",
                "title": "Synthetic Active Chain",
                "links": 8,
                "roll": {"done": 4, "countable": 10, "excluded": 0, "total": 10},
                "pct": 40.0,
            },
        ]

    monkeypatch.setattr(main_mod.db, "chain_sheets_overview", fake_chain_sheets_overview)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Synthetic Active Chain" in resp.text
    assert "Synthetic Complete Chain" not in resp.text


def test_sidebar_completion_hide_omits_completed_categories(client):
    # Side Stuff becomes 100% complete: Thing One is already done, toggle Thing Three todo -> done.
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})

    client.cookies.set("ffxiv_sidebar_completion_behavior", "hide")
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<a class="tree-link" href="/browse/Side%20Stuff">' not in resp.text
    assert '<a class="tree-link" href="/browse/Story%20Quests">' in resp.text


def test_static_pages_render(client):
    for path in (
        "/settings",
        "/credits",
        "/chains",
        "/characters",
        "/watchlist",
        "/progress-reports",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_characters_page_rename_editor_is_opt_in(client):
    resp = client.get("/characters")
    assert resp.status_code == 200
    assert "data-rename-start" in resp.text
    assert "data-rename-form hidden" in resp.text
    assert "data-rename-cancel" in resp.text


def test_characters_page_actions_order_for_inactive_character(client, conn):
    _connection, _run_id = conn
    created = client.post(
        "/characters/create",
        data={"name": "OrderCheck", "starting_class": "GLADIATOR"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    resp = client.get("/characters")
    assert resp.status_code == 200

    row_match = re.search(
        r"<tr class=\"data-row\"[^>]*>.*?OrderCheck.*?</tr>",
        resp.text,
        flags=re.DOTALL,
    )
    assert row_match is not None
    row_html = row_match.group(0)

    switch_pos = row_html.find(">Switch to<")
    rename_pos = row_html.find("data-rename-start")
    delete_pos = row_html.find(">Delete<")
    assert switch_pos != -1
    assert rename_pos != -1
    assert delete_pos != -1
    assert switch_pos < rename_pos < delete_pos


def test_character_create_requires_starting_class(client):
    resp = client.post(
        "/characters/create",
        data={"name": "NoClassCharacter", "starting_class": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "NoClassCharacter" not in resp.text
    assert "Pick an initial class from the dropdown" in resp.text


def test_character_create_persists_selected_starting_class(client, conn):
    connection, _run_id = conn

    resp = client.post(
        "/characters/create",
        data={"name": "ClassedCharacter", "starting_class": "gladiator"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    created = connection.execute(
        "SELECT starting_class FROM characters WHERE name = ?",
        ("ClassedCharacter",),
    ).fetchone()
    assert created is not None
    assert created["starting_class"] == "GLADIATOR"


def test_character_rename_route_preserves_progress(client, conn, character_id):
    connection, run_id = conn
    from app import db

    db.set_row_state(connection, character_id, run_id, "Side Stuff", 5, "done")

    resp = client.post(
        "/characters/rename",
        data={
            "character_id": str(character_id),
            "name": "Route Rename Character",
            "next_url": "/characters",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "saved=" in location

    follow = client.get(location)
    assert follow.status_code == 200
    assert "Character renamed to Route Rename Character." in follow.text

    row = connection.execute(
        "SELECT name FROM characters WHERE id = ?",
        (character_id,),
    ).fetchone()
    assert row is not None
    assert row["name"] == "Route Rename Character"
    assert db.effective_state(connection, character_id, run_id, "Side Stuff", 5) == "done"


def test_character_rename_route_validation_errors(client, conn, character_id):
    connection, _run_id = conn

    created = client.post(
        "/characters/create",
        data={"name": "Second Route Character", "starting_class": "GLADIATOR"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    second = connection.execute(
        "SELECT id FROM characters WHERE name = ?",
        ("Second Route Character",),
    ).fetchone()
    assert second is not None

    blank = client.post(
        "/characters/rename",
        data={
            "character_id": str(second["id"]),
            "name": "   ",
            "next_url": "/characters",
        },
        follow_redirects=True,
    )
    assert blank.status_code == 200
    assert "Character name is required" in blank.text

    duplicate = client.post(
        "/characters/rename",
        data={
            "character_id": str(second["id"]),
            "name": "Adventurer",
            "next_url": "/characters",
        },
        follow_redirects=True,
    )
    assert duplicate.status_code == 200
    assert "Character name already exists" in duplicate.text


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


def test_watchlist_toggle_endpoint_renders_updated_row(client):
    first = client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert first.status_code == 200
    assert "Thing Three" in first.text
    assert "Unpin" in first.text

    trigger = json.loads(first.headers["HX-Trigger"])
    assert trigger.get("progress-changed") is True

    second = client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert second.status_code == 200
    assert "Thing Three" in second.text
    assert "Pin" in second.text


def test_watchlist_page_lists_pinned_rows(client):
    client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )

    page = client.get("/watchlist")
    assert page.status_code == 200
    assert "Watchlist" in page.text
    assert "Thing Three" in page.text
    assert "/browse/Side%20Stuff?state=all#row-5" in page.text
    assert "/api/watchlist/toggle-state" in page.text
    assert "data-note-toggle" in page.text


def test_watchlist_api_toggle_state_returns_row_fragment(client, conn, character_id):
    connection, run_id = conn

    pin_resp = client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert pin_resp.status_code == 200

    entry = connection.execute(
        """
        SELECT stable_key
        FROM watchlist_entries
        WHERE character_id = ? AND sheet_name = ? AND row_index_hint = ?
        LIMIT 1
        """,
        (character_id, "Side Stuff", 5),
    ).fetchone()
    assert entry is not None

    resp = client.post(
        "/api/watchlist/toggle-state",
        data={
            "sheet_name": "Side Stuff",
            "row_index": "5",
            "stable_key": str(entry["stable_key"] or ""),
            "show_missing": "1",
        },
    )
    assert resp.status_code == 200
    assert "Thing Three" in resp.text
    assert "status-done" in resp.text

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger.get("progress-changed") is True

    after = db.effective_state(connection, character_id, run_id, "Side Stuff", 5)
    assert after == "done"


def test_row_note_upsert_and_delete_routes(client, conn, character_id):
    connection, run_id = conn

    upsert = client.post(
        "/api/row-note/upsert",
        data={
            "sheet_name": "Side Stuff",
            "row_index": "5",
            "note_text": "Remember to finish this before patch day",
            "reminder_date": "2026-06-15",
        },
    )
    assert upsert.status_code == 200
    payload = upsert.json()
    assert payload.get("ok") is True
    assert payload.get("has_note") is True
    assert payload.get("reminder_date") == "2026-06-15"

    char_name = str(db.get_character(connection, character_id)["name"])
    sidecar_doc = json.loads(
        progress_io.sidecar_path(char_name).read_text(encoding="utf-8")
    )
    notes = sidecar_doc.get("notes")
    assert isinstance(notes, list)

    node = connection.execute(
        """
        SELECT label, section_label, row_json, stable_hash
        FROM nodes
        WHERE run_id = ? AND sheet_name = ? AND row_index = ?
        LIMIT 1
        """,
        (run_id, "Side Stuff", 5),
    ).fetchone()
    assert node is not None

    ids = progress_io.compute_stable_ids(
        "Side Stuff",
        node["section_label"],
        node["label"],
        node["row_json"],
        5,
        precomputed_hash=node["stable_hash"] or None,
    )
    note_entry = progress_io.match_note_entry(notes, ids)
    assert isinstance(note_entry, dict)
    assert note_entry.get("note") == "Remember to finish this before patch day"
    assert note_entry.get("reminder_date") == "2026-06-15"

    deleted = client.post(
        "/api/row-note/delete",
        data={
            "sheet_name": "Side Stuff",
            "row_index": "5",
        },
    )
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert deleted_payload.get("ok") is True
    assert deleted_payload.get("has_note") is False

    sidecar_after = json.loads(
        progress_io.sidecar_path(char_name).read_text(encoding="utf-8")
    )
    notes_after = sidecar_after.get("notes")
    assert isinstance(notes_after, list)
    assert progress_io.match_note_entry(notes_after, ids) is None


def test_watchlist_toggle_state_route_updates_checkbox_row(client, conn, character_id):
    connection, run_id = conn

    pin_resp = client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert pin_resp.status_code == 200

    before = db.effective_state(connection, character_id, run_id, "Side Stuff", 5)
    assert before == "todo"

    toggle_resp = client.post(
        "/watchlist/toggle-state",
        data={
            "sheet_name": "Side Stuff",
            "row_index": "5",
            "next_url": "/watchlist",
        },
        follow_redirects=False,
    )
    assert toggle_resp.status_code == 303
    assert "saved=" in toggle_resp.headers.get("location", "")

    after = db.effective_state(connection, character_id, run_id, "Side Stuff", 5)
    assert after == "done"


def test_watchlist_toggle_state_route_updates_value_row(client, conn, character_id):
    connection, run_id = conn

    pin_resp = client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Classes-Jobs", "row_index": "3"},
    )
    assert pin_resp.status_code == 200

    toggle_exclude = client.post(
        "/watchlist/toggle-state",
        data={
            "sheet_name": "Classes-Jobs",
            "row_index": "3",
            "next_url": "/watchlist?show_missing=1",
        },
        follow_redirects=False,
    )
    assert toggle_exclude.status_code == 303
    assert "show_missing=1&saved=" in toggle_exclude.headers.get("location", "")

    excluded_state = db.effective_state(connection, character_id, run_id, "Classes-Jobs", 3)
    assert excluded_state == "excluded"

    toggle_restore = client.post(
        "/watchlist/toggle-state",
        data={
            "sheet_name": "Classes-Jobs",
            "row_index": "3",
            "next_url": "/watchlist",
        },
        follow_redirects=False,
    )
    assert toggle_restore.status_code == 303

    restored_state = db.effective_state(connection, character_id, run_id, "Classes-Jobs", 3)
    assert restored_state in {"todo", "done"}


def test_watchlist_unpin_route_removes_item(client, conn, character_id):
    connection, _run_id = conn

    pin_resp = client.post(
        "/api/watchlist/toggle",
        data={"sheet_name": "Side Stuff", "row_index": "5"},
    )
    assert pin_resp.status_code == 200

    entry = connection.execute(
        """
        SELECT stable_key
        FROM watchlist_entries
        WHERE character_id = ?
        LIMIT 1
        """,
        (character_id,),
    ).fetchone()
    assert entry is not None
    stable_key = str(entry["stable_key"] or "")
    assert stable_key

    unpin_resp = client.post(
        "/watchlist/unpin",
        data={
            "stable_key": stable_key,
            "next_url": "/watchlist",
        },
        follow_redirects=False,
    )
    assert unpin_resp.status_code == 303
    assert "saved=" in unpin_resp.headers.get("location", "")

    page = client.get("/watchlist")
    assert page.status_code == 200
    assert "Thing Three" not in page.text


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


def test_bulk_set_section_returns_409_when_db_locked(client, monkeypatch):
    import app.main as main_mod

    def _raise_locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main_mod.db, "set_row_state", _raise_locked)

    resp = client.post(
        "/api/bulk-set-section",
        data={
            "sheet_name": "Side Stuff",
            "target_state": "excluded",
            "row_indices_json": "[5]",
            "chain_done": "0",
        },
    )

    assert resp.status_code == 409
    assert "Database is busy with another write" in resp.text


def test_search(client):
    resp = client.get("/api/search", params={"q": "Quest"})
    assert resp.status_code == 200
    assert "Quest Alpha" in resp.text


def test_search_uses_descriptive_name_when_row_label_is_numeric(client):
    connection = db.get_connection()
    try:
        run_id = db.latest_run_id(connection)
        assert run_id is not None
        connection.execute(
            """
            UPDATE nodes
            SET label = ?, row_json = ?
            WHERE run_id = ? AND sheet_name = ? AND row_index = ?
            """,
            (
                "50",
                json.dumps({"fate": "Mint Condition"}),
                run_id,
                "Side Stuff",
                5,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    resp = client.get("/api/search", params={"q": "Mint Condition"})
    assert resp.status_code == 200
    assert "Mint Condition" in resp.text


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
    assert "ok=" in (bulk_resp.headers.get("location", ""))

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


def test_progress_report_bulk_resolution_route_reports_noop_scope(client):
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})
    report_resp = client.get(
        "/api/progress/between-run-report",
        params={"persist": "true"},
    )
    assert report_resp.status_code == 200
    report_doc = report_resp.json()
    items = report_doc.get("review_items")
    assert isinstance(items, list) and items
    character_id = int(items[0]["character_id"])

    first_bulk = client.post(
        "/progress-reports/resolve-bulk",
        data={
            "character_id": str(character_id),
            "resolution": "done",
            "only_unresolved": "1",
            "next_url": f"/progress-reports?character_id={character_id}",
        },
        follow_redirects=False,
    )
    assert first_bulk.status_code == 303

    second_bulk = client.post(
        "/progress-reports/resolve-bulk",
        data={
            "character_id": str(character_id),
            "resolution": "done",
            "only_unresolved": "1",
            "next_url": f"/progress-reports?character_id={character_id}",
        },
        follow_redirects=True,
    )
    assert second_bulk.status_code == 200
    assert "No unresolved items matched the selected bulk action scope" in second_bulk.text


def test_progress_report_bulk_resolution_scope_all_can_reopen(client):
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})
    report_resp = client.get(
        "/api/progress/between-run-report",
        params={"persist": "true"},
    )
    assert report_resp.status_code == 200
    report_doc = report_resp.json()
    items = report_doc.get("review_items")
    assert isinstance(items, list) and items
    character_id = int(items[0]["character_id"])

    resolve_all = client.post(
        "/progress-reports/resolve-bulk",
        data={
            "character_id": str(character_id),
            "resolution": "done",
            "only_unresolved": "1",
            "next_url": f"/progress-reports?character_id={character_id}",
        },
        follow_redirects=False,
    )
    assert resolve_all.status_code == 303

    reopen = client.post(
        "/progress-reports/resolve-bulk",
        data={
            "character_id": str(character_id),
            "resolution": "todo",
            "only_unresolved": "0",
            "next_url": f"/progress-reports?character_id={character_id}",
        },
        follow_redirects=False,
    )
    assert reopen.status_code == 303

    latest = progress_report.load_latest_report()
    assert isinstance(latest, dict)
    unresolved = progress_report.count_unresolved_review_items(
        latest,
        character_id=character_id,
    )
    assert unresolved >= 1


def test_progress_report_reset_baseline_route(client):
    client.post("/api/toggle", data={"sheet_name": "Side Stuff", "row_index": "5"})
    report_resp = client.get(
        "/api/progress/between-run-report",
        params={"persist": "true"},
    )
    assert report_resp.status_code == 200
    report_doc = report_resp.json()
    assert int(report_doc["summary"]["review_unresolved"]) >= 1

    reset_resp = client.post(
        "/progress-reports/reset-baseline",
        data={"next_url": "/progress-reports"},
        follow_redirects=True,
    )
    assert reset_resp.status_code == 200
    assert "Baseline reset to current progress" in reset_resp.text

    latest = progress_report.load_latest_report()
    assert isinstance(latest, dict)
    assert str(latest.get("reason") or "") == "baseline-reset"

    summary = latest.get("summary")
    assert isinstance(summary, dict)
    assert int(summary.get("review_unresolved") or 0) == 0
    assert int(summary.get("review_total") or 0) == 0


def test_progress_report_page_shows_integrity_monitor(client):
    connection = db.get_connection()
    try:
        run_id = db.latest_run_id(connection)
        assert run_id is not None

        chars = db.fetch_characters(connection)
        assert chars
        character_id = int(chars[0]["id"])

        db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 50)
        baseline = progress_report.build_snapshot(
            connection,
            run_id,
            source="test-baseline",
        )

        db.set_row_value(connection, character_id, run_id, "Classes-Jobs", 3, 5)
        report_doc, _ = progress_report.create_between_run_report(
            connection,
            run_id,
            reason="web-integrity-check",
            baseline=baseline,
            persist=True,
        )
    finally:
        connection.close()

    items = report_doc.get("review_items")
    assert isinstance(items, list)
    target_item = next(
        (
            item
            for item in items
            if str(item.get("sheet_name") or "") == "Classes-Jobs"
            and int(item.get("row_index") or 0) == 3
        ),
        None,
    )
    assert isinstance(target_item, dict)

    flags = target_item.get("integrity_flags")
    assert isinstance(flags, list)
    assert any(str(flag.get("code") or "") == "abrupt_value_drop" for flag in flags if isinstance(flag, dict))

    page_resp = client.get("/progress-reports", params={"character_id": str(character_id)})
    assert page_resp.status_code == 200
    assert "Data Integrity Monitor" in page_resp.text
    assert "Abrupt value drop" in page_resp.text
