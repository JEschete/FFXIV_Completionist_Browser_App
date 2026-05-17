#!/usr/bin/env python3
"""Generate a sidecar JSON for a test character at 100% completion.

Reads every trackable row (checkbox + value) from the latest ingest run
and writes a sidecar marking each one done. Value rows get value=100.
The app picks it up on the next startup (or via an explicit reconcile_all).

    python scripts/make_test_character.py
    python scripts/make_test_character.py --name "Speedrunner"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, progress_io  # noqa: E402


def build_full_progress(run_id: int, character_name: str) -> dict:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            """SELECT sheet_name, row_index, label, section_label, row_json,
                      COALESCE(stable_hash, '') AS stable_hash, row_type
               FROM nodes
               WHERE run_id = ? AND row_type IN ('checkbox', 'value')
               ORDER BY sheet_name, row_index""",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    now = progress_io._now_iso()
    entries: list[dict] = []
    for r in rows:
        ids = progress_io.compute_stable_ids(
            r["sheet_name"], r["section_label"], r["label"],
            r["row_json"], int(r["row_index"]),
            precomputed_hash=r["stable_hash"] or None,
        )
        entry: dict = {"ids": ids, "state": "done", "ts": now}
        if r["row_type"] == "value":
            entry["value"] = 100.0
        entries.append(entry)

    return progress_io._new_doc(
        {"name": character_name, "starting_class": None, "created_at": now},
        entries,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Test 100",
                        help="character name (default: 'Test 100')")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        run_id = db.latest_run_id(conn)
    finally:
        conn.close()
    if run_id is None:
        sys.exit("No ingest run found. Run scripts/prep_xlsx_to_sqlite.py first.")

    doc = build_full_progress(run_id, args.name)
    path = progress_io.sidecar_path(args.name)
    progress_io.save_sidecar(path, doc)
    print(f"Wrote {len(doc['progress'])} entries -> {path}")
    print("Restart the app (or trigger reconcile_all) to load this character.")


if __name__ == "__main__":
    main()
