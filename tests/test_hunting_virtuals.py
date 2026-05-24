from __future__ import annotations

import json

from app import db


def test_hunting_logs_get_prefix_virtual_subpages(conn, character_id):
    connection, run_id = conn

    connection.execute(
        """
        INSERT INTO sheets (
            run_id, sheet_index, sheet_name, title,
            is_menu, is_readonly, parent_sheet, parent_menu_section,
            data_columns_json, label_key, value_key, total_rows
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            100,
            "Logs Menu",
            "Logs Menu",
            1,
            0,
            None,
            None,
            "[]",
            None,
            None,
            0,
        ),
    )
    connection.execute(
        """
        INSERT INTO sheets (
            run_id, sheet_index, sheet_name, title,
            is_menu, is_readonly, parent_sheet, parent_menu_section,
            data_columns_json, label_key, value_key, total_rows
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            101,
            "Hunting Logs",
            "Hunting Logs",
            0,
            0,
            "Logs Menu",
            None,
            json.dumps([
                {"index": 2, "letter": "B", "key": "log", "label": "Log"},
            ]),
            "log",
            None,
            3,
        ),
    )

    rows = [
        (2, "Arcanist 01", "todo"),
        (3, "Arcanist 02", "done"),
        (4, "Archer 01", "excluded"),
    ]
    for row_index, label, state in rows:
        connection.execute(
            """
            INSERT INTO nodes (
                run_id, sheet_name, row_index, label,
                baseline_state, row_type, section_label, seq, row_json, stable_hash
            ) VALUES (?, ?, ?, ?, ?, 'checkbox', NULL, 0, ?, NULL)
            """,
            (
                run_id,
                "Hunting Logs",
                row_index,
                label,
                state,
                json.dumps({"log": label}),
            ),
        )
    connection.commit()

    sheets = db.fetch_all_sheets(connection, run_id)
    rollups = db.sheet_rollups(connection, run_id, character_id)
    tree, _overall, by_name = db.build_nav_tree(sheets, rollups)
    db.attach_content_virtual_nodes(
        connection,
        tree,
        by_name,
        run_id,
        character_id,
    )

    node = db.find_node(tree, "Hunting Logs")
    assert node is not None
    children = node["children"]
    assert [child["title"] for child in children] == ["Arcanist", "Archer"]

    arcanist = children[0]
    archer = children[1]

    assert arcanist["virtual_kind"] == "content_group"
    assert arcanist["source_sheet"] == "Hunting Logs"
    assert arcanist["row_label_prefixes"] == ["Arcanist"]
    assert arcanist["roll"] == {"done": 1, "excluded": 0, "total": 2, "countable": 2}

    assert archer["row_label_prefixes"] == ["Archer"]
    assert archer["roll"] == {"done": 0, "excluded": 1, "total": 1, "countable": 0}
