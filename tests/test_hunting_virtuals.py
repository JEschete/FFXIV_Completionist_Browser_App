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


def test_build_nav_tree_overrides_duty_journal_chronicles_section(conn, character_id):
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
            500,
            "Duty Menu - Journal",
            "Duty Menu - Journal",
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

    for idx, name, section in (
        (501, "YoRHa Dark Apocalypse", None),
        (502, "The Sorrow of Werlyt", "YoRHa: Dark Apocalypse"),
        (503, "The Arcadion", "YoRHa: Dark Apocalypse"),
    ):
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
                idx,
                name,
                name,
                0,
                0,
                "Duty Menu - Journal",
                section,
                json.dumps([{"index": 2, "letter": "B", "key": "quest", "label": "Quest"}]),
                "quest",
                None,
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO nodes (
                run_id, sheet_name, row_index, label,
                baseline_state, row_type, section_label, seq, row_json, stable_hash
            ) VALUES (?, ?, ?, ?, ?, 'checkbox', NULL, 0, ?, NULL)
            """,
            (run_id, name, 3, "Sample", "todo", json.dumps({"quest": "Sample"})),
        )

    connection.commit()

    sheets = db.fetch_all_sheets(connection, run_id)
    rollups = db.sheet_rollups(connection, run_id, character_id)
    tree, _overall, _by_name = db.build_nav_tree(sheets, rollups)

    sorrow = db.find_node(tree, "The Sorrow of Werlyt")
    arcadion = db.find_node(tree, "The Arcadion")
    yorha = db.find_node(tree, "YoRHa Dark Apocalypse")

    assert sorrow is not None
    assert arcadion is not None
    assert yorha is not None
    assert sorrow["parent_menu_section"] == "Chronicles of a New Era"
    assert arcadion["parent_menu_section"] == "Chronicles of a New Era"
    assert yorha["parent_menu_section"] == "Chronicles of a New Era"


def test_row_json_virtual_subgroups_for_crystalline_and_studium(conn, character_id):
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
            600,
            "Duty Menu - Journal",
            "Duty Menu - Journal",
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

    for idx, name in ((601, "Crystalline Mean Quests"), (602, "Studium Quests")):
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
                idx,
                name,
                name,
                0,
                0,
                "Duty Menu - Journal",
                "Class & Job Quests",
                json.dumps([{"index": 2, "letter": "B", "key": "quest", "label": "Quest"}]),
                "quest",
                None,
                0,
            ),
        )
        # Single banner in workbook data; subgrouping is derived from row_json.
        connection.execute(
            """
            INSERT INTO nodes (
                run_id, sheet_name, row_index, label,
                baseline_state, row_type, section_label, seq, row_json, stable_hash
            ) VALUES (?, ?, ?, ?, ?, 'section', ?, 0, ?, NULL)
            """,
            (run_id, name, 2, name, "todo", name, "{}"),
        )

    crystalline_rows = [
        (3, "The Crystalline Mean", "Katliss", "done"),
        (4, "For Every Child a Star", "Katliss", "todo"),
        (5, "Iola, Forgemaster", "Iola", "todo"),
        (6, "Friends of a Feather", "Bethric", "todo"),
        (7, "For Sentimental Reasons", "Thiuna", "todo"),
        (8, "Cherished Memories", "Recording Nodes", "todo"),
        (9, "On the Trail of a Myth", "Qeshi-rae", "excluded"),
        (10, "Well Eel Be Damned", "Frithrik", "todo"),
    ]
    for row_index, quest, npc, state in crystalline_rows:
        connection.execute(
            """
            INSERT INTO nodes (
                run_id, sheet_name, row_index, label,
                baseline_state, row_type, section_label, seq, row_json, stable_hash
            ) VALUES (?, ?, ?, ?, ?, 'checkbox', ?, 0, ?, NULL)
            """,
            (
                run_id,
                "Crystalline Mean Quests",
                row_index,
                quest,
                state,
                "Crystalline Mean Quests",
                json.dumps({"quest": quest, "npc": npc}),
            ),
        )

    studium_rows = [
        (3, "The Faculty", "Studium"),
        (4, "The Meeting of Minds", "Studium"),
        (5, "Fear the Thesis", "Aetherology"),
        (6, "Cultured Pursuits", "Anthropology"),
        (7, "Professor Rurusha's New Friend", "Archaeology"),
        (8, "In Search of the Azure Star", "Astronomy"),
        (9, "Perfectly Awful", "Medicine"),
    ]
    for row_index, quest, faculty in studium_rows:
        connection.execute(
            """
            INSERT INTO nodes (
                run_id, sheet_name, row_index, label,
                baseline_state, row_type, section_label, seq, row_json, stable_hash
            ) VALUES (?, ?, ?, ?, 'todo', 'checkbox', ?, 0, ?, NULL)
            """,
            (
                run_id,
                "Studium Quests",
                row_index,
                quest,
                "Crystalline Mean Quests",
                json.dumps({"quest": quest, "faculty": faculty}),
            ),
        )

    connection.commit()

    sheets = db.fetch_all_sheets(connection, run_id)
    rollups = db.sheet_rollups(connection, run_id, character_id)
    tree, _overall, by_name = db.build_nav_tree(sheets, rollups)
    db.attach_content_virtual_nodes(connection, tree, by_name, run_id, character_id)

    crystalline = db.find_node(tree, "Crystalline Mean Quests")
    studium = db.find_node(tree, "Studium Quests")
    assert crystalline is not None
    assert studium is not None

    crystalline_titles = [child["title"] for child in crystalline["children"]]
    assert crystalline_titles == [
        "Crystalline Mean",
        "Facet of Forging",
        "Facet of Crafting",
        "Facet of Nourishing",
        "Facet of Gathering",
        "Facet of Fishing",
    ]
    nourishing = next(child for child in crystalline["children"] if child["title"] == "Facet of Nourishing")
    assert nourishing["row_indexes"] == [7, 8]

    studium_titles = [child["title"] for child in studium["children"]]
    assert studium_titles == [
        "Studium",
        "Faculty of Aetherology",
        "Faculty of Anthropology",
        "Faculty of Archaeology",
        "Faculty of Astronomy",
        "Faculty of Medicine",
    ]
    studium_group = next(child for child in studium["children"] if child["title"] == "Studium")
    assert studium_group["row_indexes"] == [3, 4]
