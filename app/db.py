"""Data layer for the FFXIV completion tracker.

Schema (produced by scripts/prep_xlsx_to_sqlite.py):
  ingest_runs, sheets, nodes, edges, characters, character_progress

Effective state of a row = the character's override (character_progress.state)
or, falling back, the workbook baseline (nodes.baseline_state).
States: 'done' | 'todo' | 'excluded'. Excluded rows leave the denominator.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app import section_sort

DB_PATH = Path("data/ffxiv_tracker.sqlite")
VALUE_CAPS_PATH = Path("data/value_caps.json")

_VALUE_CAPS_CACHE_MTIME_NS: int | None = None
_VALUE_CAPS_CACHE_DATA: dict[str, float] = {}

# state cycle used by the toggle endpoint
NEXT_STATE = {"todo": "done", "done": "excluded", "excluded": "todo"}

# Starting classes the workbook recognizes (kept in sync with the prep script).
STARTING_CLASSES = (
    "ARCANIST", "ARCHER", "CONJURER", "GLADIATOR",
    "LANCER", "MARAUDER", "PUGILIST", "THAUMATURGE",
)


def _norm_text(value: str | None) -> str:
    text = (value or "").replace("\xa0", " ").strip().lower()
    return re.sub(r"\s+", " ", text)


def _value_cap_key(sheet_name: str, section_label: str | None, label: str | None) -> str:
    return "|".join((
        _norm_text(sheet_name),
        _norm_text(section_label),
        _norm_text(label),
    ))


def _default_value_cap(sheet_name: str, section_label: str | None, label: str | None) -> float:
    sheet_norm = _norm_text(sheet_name)
    section_norm = _norm_text(section_label)
    label_norm = _norm_text(label)

    if sheet_norm == "classes-jobs":
        if "desynthesis" in section_norm:
            return 770.0
        if "blue mage" in label_norm:
            return 80.0
    return 100.0


def load_value_cap_overrides() -> dict[str, float]:
    global _VALUE_CAPS_CACHE_MTIME_NS, _VALUE_CAPS_CACHE_DATA

    try:
        stat = VALUE_CAPS_PATH.stat()
    except OSError:
        _VALUE_CAPS_CACHE_MTIME_NS = None
        _VALUE_CAPS_CACHE_DATA = {}
        return {}

    mtime_ns = int(stat.st_mtime_ns)
    if _VALUE_CAPS_CACHE_MTIME_NS == mtime_ns:
        return dict(_VALUE_CAPS_CACHE_DATA)

    parsed: dict[str, float] = {}
    try:
        raw = json.loads(VALUE_CAPS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}

    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                cap = float(value)
            except (TypeError, ValueError):
                continue
            if cap > 0:
                parsed[str(key)] = cap

    _VALUE_CAPS_CACHE_MTIME_NS = mtime_ns
    _VALUE_CAPS_CACHE_DATA = parsed
    return dict(parsed)


def save_value_cap_overrides(overrides: dict[str, float]) -> dict[str, float]:
    global _VALUE_CAPS_CACHE_MTIME_NS, _VALUE_CAPS_CACHE_DATA

    cleaned: dict[str, float] = {}
    for key, value in overrides.items():
        try:
            cap = float(value)
        except (TypeError, ValueError):
            continue
        if cap <= 0:
            continue
        cleaned[str(key)] = cap

    VALUE_CAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VALUE_CAPS_PATH.write_text(
        json.dumps(cleaned, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    try:
        _VALUE_CAPS_CACHE_MTIME_NS = int(VALUE_CAPS_PATH.stat().st_mtime_ns)
    except OSError:
        _VALUE_CAPS_CACHE_MTIME_NS = None
    _VALUE_CAPS_CACHE_DATA = dict(cleaned)
    return dict(cleaned)


def resolve_value_cap(
    sheet_name: str,
    section_label: str | None,
    label: str | None,
) -> float:
    defaults = _default_value_cap(sheet_name, section_label, label)
    overrides = load_value_cap_overrides()
    key = _value_cap_key(sheet_name, section_label, label)
    return float(overrides.get(key, defaults))


def value_row_cap(
    conn: sqlite3.Connection,
    run_id: int,
    sheet_name: str,
    row_index: int,
) -> float:
    row = conn.execute(
        """
        SELECT label, section_label, row_type
        FROM nodes
        WHERE run_id = ? AND sheet_name = ? AND row_index = ?
        """,
        (run_id, sheet_name, row_index),
    ).fetchone()
    if row is None or row["row_type"] != "value":
        return 100.0
    return resolve_value_cap(
        sheet_name,
        row["section_label"],
        row["label"],
    )


def classes_jobs_cap_rows(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT row_index, label, section_label, row_json
        FROM nodes
        WHERE run_id = ? AND sheet_name = 'Classes-Jobs' AND row_type = 'value'
        ORDER BY row_index
        """,
        (run_id,),
    ).fetchall()

    parsed: list[dict[str, Any]] = []
    name_counts: dict[str, int] = {}
    for row in rows:
        payload = json.loads(row["row_json"] or "{}")
        label = str(row["label"] or payload.get("job_class") or "").strip()
        section_label = str(row["section_label"] or "").strip()
        name_counts[label] = name_counts.get(label, 0) + 1
        parsed.append(
            {
                "row_index": int(row["row_index"]),
                "label": label,
                "section_label": section_label,
            }
        )

    out: list[dict[str, Any]] = []
    for item in parsed:
        label = item["label"]
        section_label = item["section_label"]
        if name_counts.get(label, 0) > 1 and section_label:
            display_name = f"{label} ({section_label})"
        else:
            display_name = label

        default_cap = _default_value_cap("Classes-Jobs", section_label, label)
        current_cap = resolve_value_cap("Classes-Jobs", section_label, label)
        cap_key = _value_cap_key("Classes-Jobs", section_label, label)
        out.append(
            {
                "row_index": item["row_index"],
                "display_name": display_name,
                "label": label,
                "section_label": section_label,
                "cap_key": cap_key,
                "default_cap": int(default_cap) if float(default_cap).is_integer() else default_cap,
                "current_cap": int(current_cap) if float(current_cap).is_integer() else current_cap,
            }
        )
    return out


def _state_clauses(starting_class: str | None) -> tuple[str, str, list]:
    """Return (eff_expression, extra_join_sql, extra_join_params) used to splice
    class-overlay support into queries. With no class chosen, behavior is
    identical to plain `COALESCE(progress, baseline)`."""
    if not starting_class:
        return "COALESCE(p.state, n.baseline_state)", "", []
    return (
        "COALESCE(p.state, co.state, n.baseline_state)",
        ("LEFT JOIN class_overrides co "
         "ON co.run_id = n.run_id AND co.sheet_name = n.sheet_name "
         "AND co.row_index = n.row_index AND co.starting_class = ?"),
        [starting_class],
    )

# Per-(character, run, sheet) cached rollup. Seeded on first read and kept in
# sync by set_row_state via a +/- delta — so a toggle pays one indexed write
# instead of a 37k-row aggregation. countable = total - excluded.
ROLLUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress_rollup (
    character_id INTEGER NOT NULL,
    run_id       INTEGER NOT NULL,
    sheet_name   TEXT NOT NULL,
    done         INTEGER NOT NULL DEFAULT 0,
    excluded     INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (character_id, run_id, sheet_name)
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(ROLLUP_SCHEMA)  # idempotent; bootstraps on first connect
    return conn


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    """Return the newest ingest run id, or None if there hasn't been one
    yet. A fresh / never-prepped DB is allowed — the table simply doesn't
    exist and we treat that the same as "no runs"."""
    try:
        row = conn.execute("SELECT MAX(id) AS m FROM ingest_runs").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row["m"]) if row and row["m"] is not None else None


# --- characters -------------------------------------------------------------

def fetch_characters(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, name, starting_class, created_at FROM characters ORDER BY id"
    ))


def get_character(conn: sqlite3.Connection, character_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, starting_class, created_at FROM characters WHERE id = ?",
        (character_id,),
    ).fetchone()


def create_character(conn: sqlite3.Connection, name: str) -> int:
    clean = name.strip()
    if not clean:
        raise ValueError("Character name is required.")
    cur = conn.execute(
        "INSERT INTO characters (name, created_at) VALUES (?, ?)", (clean, now())
    )
    conn.commit()
    if cur.lastrowid is None:
        raise ValueError(f"Could not create character '{clean}'")
    return int(cur.lastrowid)


def delete_character(conn: sqlite3.Connection, character_id: int) -> int:
    total = conn.execute("SELECT COUNT(*) AS c FROM characters").fetchone()["c"]
    if total <= 1:
        raise ValueError("Cannot delete the last character.")
    victim = conn.execute(
        "SELECT name FROM characters WHERE id = ?",
        (character_id,),
    ).fetchone()
    victim_name = str(victim["name"]) if victim and victim["name"] else None

    conn.execute("DELETE FROM progress_rollup WHERE character_id = ?", (character_id,))
    conn.execute("DELETE FROM character_progress WHERE character_id = ?", (character_id,))
    conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))
    conn.commit()

    if victim_name:
        from app import progress_io

        remaining_names = [
            str(r["name"])
            for r in conn.execute(
                "SELECT name FROM characters WHERE name IS NOT NULL"
            ).fetchall()
            if r["name"]
        ]
        sidecar = progress_io.sidecar_path_for_delete(
            victim_name,
            other_character_names=remaining_names,
        )
        if sidecar is not None:
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
            progress_io.invalidate_cache(sidecar)

    return int(conn.execute("SELECT MIN(id) AS m FROM characters").fetchone()["m"])


def set_character_class(
    conn: sqlite3.Connection, character_id: int, starting_class: str | None
) -> None:
    """Update the character's starting class. Drops the cached rollup so it
    re-seeds on the next read with the new class overlay."""
    if starting_class and starting_class not in STARTING_CLASSES:
        raise ValueError(f"Unknown starting class: {starting_class}")
    conn.execute(
        "UPDATE characters SET starting_class = ? WHERE id = ?",
        (starting_class, character_id),
    )
    conn.execute(
        "DELETE FROM progress_rollup WHERE character_id = ?", (character_id,)
    )
    conn.commit()


def resolve_active_character(
    conn: sqlite3.Connection, requested_id: int | None
) -> sqlite3.Row:
    if requested_id is not None:
        row = get_character(conn, requested_id)
        if row is not None:
            return row
    row = conn.execute(
        "SELECT id, name, starting_class, created_at FROM characters ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return row
    new_id = create_character(conn, "Adventurer")
    found = get_character(conn, new_id)
    assert found is not None
    return found


# --- progress writes --------------------------------------------------------

def _ensure_rollup_seeded(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    starting_class: str | None = None,
) -> None:
    """Populate progress_rollup for (character, run) if it's empty.
    Done once, then incremental deltas keep it accurate. The seed snapshots
    the class overlay too — when starting_class changes, callers should clear
    the rollup so the next read re-seeds with the new overlay."""
    seen = conn.execute(
        "SELECT 1 FROM progress_rollup WHERE character_id=? AND run_id=? LIMIT 1",
        (character_id, run_id),
    ).fetchone()
    if seen:
        return
    eff, join, jparams = _state_clauses(starting_class)
    conn.execute(
        f"""
        INSERT INTO progress_rollup (character_id, run_id, sheet_name, done, excluded, total)
        SELECT ?, ?, sheet_name,
               SUM(CASE WHEN eff = 'done' THEN 1 ELSE 0 END),
               SUM(CASE WHEN eff = 'excluded' THEN 1 ELSE 0 END),
               COUNT(*)
        FROM (
            SELECT n.sheet_name, {eff} AS eff
            FROM nodes n
            LEFT JOIN character_progress p
              ON p.character_id = ? AND p.run_id = n.run_id
             AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
            {join}
            WHERE n.run_id = ? AND n.row_type IN ('checkbox', 'value')
        )
        GROUP BY sheet_name
        """,
        (character_id, run_id, character_id, *jparams, run_id),
    )


def set_row_state(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    state: str,
    progress_percent: float | None = None,
    *,
    commit: bool = True,
    starting_class: str | None = None,
) -> None:
    if state not in ("done", "todo", "excluded"):
        raise ValueError(f"Bad state: {state}")

    # Acquire a write transaction before reading old effective state. This
    # serializes concurrent same-row writes across connections so rollup delta
    # math doesn't race on stale old_eff snapshots.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")

    # seed the rollup BEFORE we write — otherwise a lazy seed would read the
    # post-write state and the +/- delta below would double-count it
    _ensure_rollup_seeded(conn, character_id, run_id, starting_class)

    # capture the previous effective state so we can update the cached rollup
    # transactionally with the same write
    eff, join, jparams = _state_clauses(starting_class)
    prev = conn.execute(
        f"""
        SELECT {eff} AS eff
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ? AND n.row_index = ?
        """,
        (character_id, *jparams, run_id, sheet_name, row_index),
    ).fetchone()
    old_eff = prev["eff"] if prev else "todo"

    # a write that equals the baseline is stored anyway so toggles are explicit.
    # progress_percent is preserved across writes that don't supply one — so
    # toggling a value row to excluded and back keeps its level intact.
    conn.execute(
        """
        INSERT INTO character_progress
            (character_id, run_id, sheet_name, row_index, state, progress_percent, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(character_id, run_id, sheet_name, row_index) DO UPDATE SET
            state = excluded.state,
            progress_percent = COALESCE(excluded.progress_percent, character_progress.progress_percent),
            updated_at = excluded.updated_at
        """,
        (character_id, run_id, sheet_name, row_index, state, progress_percent, now()),
    )

    # apply +/- delta to the cached rollup; total never moves (per-row count)
    if old_eff != state:
        d_done = (1 if state == "done" else 0) - (1 if old_eff == "done" else 0)
        d_excl = (1 if state == "excluded" else 0) - (1 if old_eff == "excluded" else 0)
        if d_done or d_excl:
            conn.execute(
                """
                UPDATE progress_rollup
                SET done = done + ?, excluded = excluded + ?
                WHERE character_id = ? AND run_id = ? AND sheet_name = ?
                """,
                (d_done, d_excl, character_id, run_id, sheet_name),
            )

    # Write through to the per-character JSON sidecar after the DB upsert.
    # progress_io owns the sparse / tiered-identity / atomic-write logic so
    # this module stays focused on SQL. Lazy import avoids a top-level cycle
    # if progress_io ever needs anything from db.
    from app import progress_io
    try:
        progress_io.record_state_change(
            conn, character_id, run_id, sheet_name, row_index, state,
            progress_percent=progress_percent,
        )
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise


def clear_row_override(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    *,
    commit: bool = True,
    starting_class: str | None = None,
) -> bool:
    """Remove a character override for a row, restoring baseline/inherited state.

    Returns True when an explicit character_progress row was removed.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")

    _ensure_rollup_seeded(conn, character_id, run_id, starting_class)

    eff, join, jparams = _state_clauses(starting_class)
    prev = conn.execute(
        f"""
        SELECT {eff} AS eff
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ? AND n.row_index = ?
        """,
        (character_id, *jparams, run_id, sheet_name, row_index),
    ).fetchone()
    old_eff = prev["eff"] if prev else "todo"

    cur = conn.execute(
        """
        DELETE FROM character_progress
        WHERE character_id = ? AND run_id = ? AND sheet_name = ? AND row_index = ?
        """,
        (character_id, run_id, sheet_name, row_index),
    )
    removed = cur.rowcount > 0

    now_eff_row = conn.execute(
        f"""
        SELECT {eff} AS eff
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ? AND n.row_index = ?
        """,
        (character_id, *jparams, run_id, sheet_name, row_index),
    ).fetchone()
    new_eff = now_eff_row["eff"] if now_eff_row else "todo"

    if old_eff != new_eff:
        d_done = (1 if new_eff == "done" else 0) - (1 if old_eff == "done" else 0)
        d_excl = (1 if new_eff == "excluded" else 0) - (1 if old_eff == "excluded" else 0)
        if d_done or d_excl:
            conn.execute(
                """
                UPDATE progress_rollup
                SET done = done + ?, excluded = excluded + ?
                WHERE character_id = ? AND run_id = ? AND sheet_name = ?
                """,
                (d_done, d_excl, character_id, run_id, sheet_name),
            )

    try:
        if removed:
            from app import progress_io

            progress_io.remove_state_change(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
            )
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        raise

    return removed


def effective_state(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None = None,
) -> str:
    eff, join, jparams = _state_clauses(starting_class)
    row = conn.execute(
        f"""
        SELECT {eff} AS eff
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ? AND n.row_index = ?
        """,
        (character_id, *jparams, run_id, sheet_name, row_index),
    ).fetchone()
    return row["eff"] if row else "todo"


def is_chain_row(
    conn: sqlite3.Connection, run_id: int, sheet_name: str, row_index: int
) -> bool:
    """A row is part of a real prerequisite chain iff it has an incoming
    sequence edge (ingest only emits those inside chain sections)."""
    return conn.execute(
        """SELECT 1 FROM edges
           WHERE run_id=? AND sheet_name=? AND edge_type='sequence'
             AND target_row_index=? LIMIT 1""",
        (run_id, sheet_name, row_index),
    ).fetchone() is not None


def toggle_row(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None = None,
) -> tuple[str, list[int]]:
    """Cycle todo -> done -> excluded -> todo. For chain rows, cascade in
    both directions:

    * **todo → done**: walk backward, marking prereqs done.
    * **done → excluded** (or done → anything-else): walk forward, reverting
      any currently-done downstream rows to todo so the chain stays
      logically consistent.
    * **excluded → todo**: no cascade (the row was already non-done; chain
      state unchanged).

    Non-chain rows always return [row_index] with no cascading."""
    cur = effective_state(
        conn, character_id, run_id, sheet_name, row_index, starting_class
    )
    new_state = NEXT_STATE.get(cur, "done")
    is_chain = is_chain_row(conn, run_id, sheet_name, row_index)

    if is_chain and new_state == "done":
        changed = complete_with_prerequisites(
            conn, character_id, run_id, sheet_name, row_index, starting_class
        )
        # complete_with_prerequisites only lists *transitions*; if the click
        # target itself was already done somehow, ensure it shows up so the
        # UI re-renders it.
        if row_index not in changed:
            changed.append(row_index)
        return new_state, changed

    if is_chain and cur == "done":
        changed = revert_with_successors(
            conn, character_id, run_id, sheet_name, row_index, new_state,
            starting_class,
        )
        if row_index not in changed:
            changed.append(row_index)
        return new_state, changed

    set_row_state(
        conn, character_id, run_id, sheet_name, row_index, new_state,
        starting_class=starting_class,
    )
    return new_state, [row_index]


def set_row_value(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    percent: float,
    *,
    commit: bool = True,
    starting_class: str | None = None,
) -> str:
    """Set a row's numeric value (0..cap). Derives state from that row's cap:
    cap+ → done; <cap → todo; never auto-unexcludes."""
    cap = value_row_cap(conn, run_id, sheet_name, row_index)
    pct = max(0.0, min(cap, float(percent)))
    cur = effective_state(
        conn, character_id, run_id, sheet_name, row_index, starting_class
    )
    if cur == "excluded":
        new_state = "excluded"
    elif pct >= cap:
        new_state = "done"
    else:
        new_state = "todo"
    set_row_state(
        conn, character_id, run_id, sheet_name, row_index, new_state,
        progress_percent=pct, commit=commit, starting_class=starting_class,
    )
    return new_state


def toggle_excluded(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None = None,
) -> str:
    """Two-state toggle for value rows: excluded ↔ natural state.
    When un-excluding, derives state from the saved percent."""
    cur = effective_state(
        conn, character_id, run_id, sheet_name, row_index, starting_class
    )
    if cur == "excluded":
        cap = value_row_cap(conn, run_id, sheet_name, row_index)
        prev = conn.execute(
            """SELECT progress_percent FROM character_progress
               WHERE character_id=? AND run_id=? AND sheet_name=? AND row_index=?""",
            (character_id, run_id, sheet_name, row_index),
        ).fetchone()
        pct = float(prev["progress_percent"]) if prev and prev["progress_percent"] else 0.0
        new = "done" if pct >= cap else "todo"
    else:
        new = "excluded"
    set_row_state(
        conn, character_id, run_id, sheet_name, row_index, new,
        starting_class=starting_class,
    )
    return new


def complete_with_prerequisites(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None = None,
) -> list[int]:
    """Mark a row done and walk 'sequence' edges backward, completing the chain.
    Returns the list of row_indexes whose state changed (one commit at the end).

    The sidecar write is batched: every cascaded set_row_state updates the
    in-memory doc, and a single disk save happens when the batch context exits.
    Saves a 22-row cascade from 22 × ~280 ms down to one ~250 ms write."""
    from app import progress_io

    changed: list[int] = []
    with progress_io.batch(conn, character_id):
        if effective_state(
            conn, character_id, run_id, sheet_name, row_index, starting_class
        ) != "done":
            set_row_state(
                conn, character_id, run_id, sheet_name, row_index, "done",
                commit=False, starting_class=starting_class,
            )
            changed.append(row_index)

        seen = {row_index}
        stack = [row_index]
        while stack:
            current = stack.pop()
            prereqs = conn.execute(
                """
                SELECT source_row_index FROM edges
                WHERE run_id = ? AND sheet_name = ? AND edge_type = 'sequence'
                  AND target_row_index = ? AND source_row_index IS NOT NULL
                """,
                (run_id, sheet_name, current),
            ).fetchall()
            for pr in prereqs:
                idx = int(pr["source_row_index"])
                if idx in seen:
                    continue
                seen.add(idx)
                if effective_state(
                    conn, character_id, run_id, sheet_name, idx, starting_class
                ) != "done":
                    set_row_state(
                        conn, character_id, run_id, sheet_name, idx, "done",
                        commit=False, starting_class=starting_class,
                    )
                    changed.append(idx)
                stack.append(idx)

    if changed:
        conn.commit()
    return changed


def revert_with_successors(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    new_state: str,
    starting_class: str | None = None,
) -> list[int]:
    """Set a row to a non-done state (todo or excluded); for any chain row
    *downstream* via 'sequence' edges that's currently 'done', revert it to
    'todo' since its prerequisite is no longer satisfied. Mirrors
    ``complete_with_prerequisites`` for the un-completing direction.

    Excluded successors are left alone — that was an explicit user choice.
    Returns the list of row_indexes whose state changed."""
    if new_state not in ("todo", "excluded"):
        raise ValueError(
            f"revert_with_successors expects todo|excluded, got {new_state!r}"
        )
    from app import progress_io

    changed: list[int] = []
    with progress_io.batch(conn, character_id):
        if effective_state(
            conn, character_id, run_id, sheet_name, row_index, starting_class
        ) != new_state:
            set_row_state(
                conn, character_id, run_id, sheet_name, row_index, new_state,
                commit=False, starting_class=starting_class,
            )
            changed.append(row_index)

        seen = {row_index}
        stack = [row_index]
        while stack:
            current = stack.pop()
            successors = conn.execute(
                """
                SELECT target_row_index FROM edges
                WHERE run_id = ? AND sheet_name = ? AND edge_type = 'sequence'
                  AND source_row_index = ? AND target_row_index IS NOT NULL
                """,
                (run_id, sheet_name, current),
            ).fetchall()
            for nxt in successors:
                idx = int(nxt["target_row_index"])
                if idx in seen:
                    continue
                seen.add(idx)
                # Only revert successors that are currently done. Leave excluded
                # ones alone (the user explicitly chose that). Walk past either
                # way — a downstream done row beyond a non-done gap still needs
                # to revert because its own prereq path is now broken.
                if effective_state(
                    conn, character_id, run_id, sheet_name, idx, starting_class
                ) == "done":
                    set_row_state(
                        conn, character_id, run_id, sheet_name, idx, "todo",
                        commit=False, starting_class=starting_class,
                    )
                    changed.append(idx)
                stack.append(idx)

    if changed:
        conn.commit()
    return changed


def _section_trackable_rollups(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    sheet_name: str,
    section_row_indexes: list[int],
    starting_class: str | None = None,
) -> dict[int, dict[str, int]]:
    """Trackable row rollups keyed by section header row index.

    This avoids parsing row_json for every sheet row when building virtual
    content groups in the sidebar.
    """
    section_indexes = sorted({int(i) for i in section_row_indexes})
    if not section_indexes:
        return {}

    eff, join, jparams = _state_clauses(starting_class)
    rows = conn.execute(
        f"""
        SELECT n.row_index, {eff} AS eff
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ?
          AND n.row_type IN ('checkbox', 'value')
        ORDER BY n.row_index
        """,
        (character_id, *jparams, run_id, sheet_name),
    ).fetchall()

    rollups = {idx: _empty_roll() for idx in section_indexes}
    section_cursor = 0
    current_section: int | None = None
    for row in rows:
        row_index = int(row["row_index"])
        while section_cursor < len(section_indexes) and section_indexes[section_cursor] < row_index:
            current_section = section_indexes[section_cursor]
            section_cursor += 1
        if current_section is None:
            continue
        roll = rollups.get(current_section)
        if roll is None:
            continue

        roll["total"] += 1
        eff_state = str(row["eff"] or "todo")
        if eff_state == "done":
            roll["done"] += 1
        elif eff_state == "excluded":
            roll["excluded"] += 1

    for roll in rollups.values():
        roll["countable"] = roll["total"] - roll["excluded"]
    return rollups


# --- sheet / node reads -----------------------------------------------------

def fetch_sheet(conn: sqlite3.Connection, run_id: int, sheet_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sheets WHERE run_id = ? AND sheet_name = ?",
        (run_id, sheet_name),
    ).fetchone()


def fetch_all_sheets(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM sheets WHERE run_id = ? ORDER BY sheet_index", (run_id,)
        )
    )


def _sheet_rollups_live(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    starting_class: str | None = None,
) -> dict[str, dict[str, int]]:
    """Read rollups directly from nodes+character_progress without touching
    the cached progress_rollup table. Used as a lock-safe fallback while a
    long-running writer holds the DB write lock."""
    eff, join, jparams = _state_clauses(starting_class)
    rows = conn.execute(
        f"""
        SELECT sheet_name,
               SUM(CASE WHEN eff = 'done' THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN eff = 'excluded' THEN 1 ELSE 0 END) AS excluded,
               COUNT(*) AS total
        FROM (
            SELECT n.sheet_name, {eff} AS eff
            FROM nodes n
            LEFT JOIN character_progress p
              ON p.character_id = ? AND p.run_id = n.run_id
             AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
            {join}
            WHERE n.run_id = ? AND n.row_type IN ('checkbox', 'value')
        )
        GROUP BY sheet_name
        """,
        (character_id, *jparams, run_id),
    ).fetchall()
    return {
        r["sheet_name"]: {
            "done": r["done"],
            "excluded": r["excluded"],
            "total": r["total"],
            "countable": r["total"] - r["excluded"],
        }
        for r in rows
    }


def sheet_rollups(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    starting_class: str | None = None,
) -> dict[str, dict[str, int]]:
    """Per-sheet {done, excluded, total, countable} for trackable rows.

    Reads the cached `progress_rollup` table — populated lazily and kept in sync
    by set_row_state's delta updates, so this is one indexed scan over ~229
    rows instead of an aggregation over the full nodes set.
    """
    try:
        _ensure_rollup_seeded(conn, character_id, run_id, starting_class)
        # Seeding inside _ensure_rollup_seeded opens a transaction on first INSERT.
        # Commit unconditionally so a read-only caller (e.g. a browse render) still
        # persists the seed when the connection closes. No-op if nothing changed.
        conn.commit()
    except sqlite3.OperationalError as exc:
        # During large imports another connection can hold a write transaction.
        # Fall back to a read-only rollup query so page renders keep working.
        if "locked" not in str(exc).lower():
            raise
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return _sheet_rollups_live(conn, run_id, character_id, starting_class)

    rows = conn.execute(
        """
        SELECT sheet_name, done, excluded, total
        FROM progress_rollup
        WHERE character_id = ? AND run_id = ?
        """,
        (character_id, run_id),
    ).fetchall()
    return {
        r["sheet_name"]: {
            "done": r["done"],
            "excluded": r["excluded"],
            "total": r["total"],
            "countable": r["total"] - r["excluded"],
        }
        for r in rows
    }


def _empty_roll() -> dict[str, int]:
    return {"done": 0, "excluded": 0, "total": 0, "countable": 0}


def pct(roll: dict[str, int]) -> float:
    return round(100.0 * roll["done"] / roll["countable"], 2) if roll["countable"] else 0.0


# Delimiter between a real menu's sheet_name and a virtual section node's
# label, e.g. ``"Duty Menu - Journal|Main Scenario (ARR through Endwalker)"``.
# Picked because ``|`` is URL-safe and never appears in a workbook sheet name.
VIRTUAL_SEP = "|"

CONTENT_VIRTUAL_PREFIX = "content-group:"

_PAGE_SECTION_RE = re.compile(r"^PAGE\s+\d+$", re.IGNORECASE)

_GRAND_COMPANY_HEADERS = {
    "maelstrom": "Storm",
    "order of the twin adder": "Serpent",
    "immortal flames": "Flame",
}

_HUNTING_LOG_LABEL_RE = re.compile(r"^(.+?)\s+\d{1,3}$")


def _slugify(text: str) -> str:
    raw = (text or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return raw or "group"


def _is_likely_sheet_header(label: str, sheet_title: str) -> bool:
    norm_label = " ".join((label or "").strip().lower().split())
    norm_title = " ".join((sheet_title or "").strip().lower().split())
    if not norm_label:
        return True
    if norm_label == norm_title:
        return True
    if norm_label.endswith(" logs") and norm_title.endswith(" logs"):
        return True
    if norm_label.endswith(" log") and norm_title.endswith(" log"):
        return True
    if norm_label.endswith(" guide") and norm_title.endswith(" guide"):
        return True
    return False


def _hunting_log_label_prefixes(conn: sqlite3.Connection, run_id: int) -> list[str]:
    """Ordered unique class/job prefixes inferred from Hunting Logs labels.

    Hunting Logs rows are shaped like "Arcanist 01" with no explicit section
    banners, so derive virtual subgroup labels from the shared text prefix.
    """
    rows = conn.execute(
        """
        SELECT label
        FROM nodes
        WHERE run_id = ? AND sheet_name = 'Hunting Logs'
          AND row_type IN ('checkbox', 'value')
        ORDER BY row_index
        """,
        (run_id,),
    ).fetchall()

    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        label = str(row["label"] or "").strip()
        if not label:
            continue
        match = _HUNTING_LOG_LABEL_RE.match(label)
        if not match:
            continue
        prefix = " ".join(match.group(1).split())
        norm = prefix.lower()
        if not prefix or norm in seen:
            continue
        seen.add(norm)
        out.append(prefix)
    return out


def _content_virtual_specs_for_sheet(
    sheet_name: str,
    sheet_title: str,
    sections: list[dict],
) -> list[dict]:
    """Return virtual subgroup specs for a content sheet.

    Each spec is ``{"title": str, "section_row_indexes": [int, ...]}`` and
    maps to one synthetic child page in the sidebar.
    """
    if len(sections) < 2:
        return []

    # 0) Grand Company split (specific user-facing labels).
    # Workbook headers are MAELSTROM / ORDER OF THE TWIN ADDER / IMMORTAL FLAMES,
    # but the desired sidebar labels are Storm / Serpent / Flame.
    company_headers: list[tuple[int, str]] = []
    for idx, sec in enumerate(sections):
        norm = " ".join(str(sec.get("label") or "").strip().lower().split())
        label = _GRAND_COMPANY_HEADERS.get(norm)
        if label is not None:
            company_headers.append((idx, label))

    if len(company_headers) >= 2:
        groups: list[dict] = []
        for n, (start_idx, title) in enumerate(company_headers):
            end_idx = company_headers[n + 1][0] if (n + 1) < len(company_headers) else len(sections)
            span = sections[start_idx:end_idx]
            section_row_indexes = [int(s["row_index"]) for s in span]
            if section_row_indexes:
                groups.append(
                    {
                        "title": title,
                        "section_row_indexes": section_row_indexes,
                    }
                )
        if groups:
            return groups

    # 1) Track-based split (Miner/Botanist style: mining vs quarrying, etc.).
    track_order: list[str] = []
    track_sections: dict[str, list[int]] = {}
    for sec in sections:
        meta_raw = sec.get("meta")
        meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
        track_raw = meta.get("track")
        track = str(track_raw).strip().lower() if track_raw is not None else ""
        if not track:
            label_norm = str(sec.get("label") or "").upper()
            if "QUARRY" in label_norm:
                track = "quarrying"
            elif "HARVEST" in label_norm:
                track = "harvesting"
            elif "LOGGING" in label_norm:
                track = "logging"
            elif "MINING" in label_norm:
                track = "mining"
        if track not in {"mining", "quarrying", "logging", "harvesting"}:
            continue
        if track not in track_sections:
            track_sections[track] = []
            track_order.append(track)
        track_sections[track].append(int(sec["row_index"]))

    if len(track_order) >= 2:
        return [
            {
                "title": track.title(),
                "section_row_indexes": track_sections[track],
            }
            for track in track_order
        ]

    # 2) Expansion/chapter split when PAGE labels repeat under parent headers.
    page_labels = [
        sec["label"] for sec in sections
        if _PAGE_SECTION_RE.match(str(sec.get("label") or "").strip())
    ]
    if len(page_labels) < 2:
        return []

    counts: dict[str, int] = {}
    for label in page_labels:
        counts[label] = counts.get(label, 0) + 1
    if max(counts.values(), default=0) < 2:
        return []

    non_page_indexes = [
        i for i, sec in enumerate(sections)
        if not _PAGE_SECTION_RE.match(str(sec.get("label") or "").strip())
    ]
    groups: list[dict] = []
    for n, idx in enumerate(non_page_indexes):
        sec = sections[idx]
        label = str(sec.get("label") or "").strip()
        if not label:
            continue

        next_non_page_idx = (
            non_page_indexes[n + 1] if (n + 1) < len(non_page_indexes) else len(sections)
        )
        pages_in_span = [
            sections[i]
            for i in range(idx + 1, next_non_page_idx)
            if _PAGE_SECTION_RE.match(str(sections[i].get("label") or "").strip())
        ]
        if not pages_in_span:
            continue
        if _is_likely_sheet_header(label, sheet_title):
            continue

        groups.append(
            {
                "title": label.title(),
                "section_row_indexes": [int(p["row_index"]) for p in pages_in_span],
            }
        )

    return groups if len(groups) >= 2 else []


def _label_prefix_trackable_rollups(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    sheet_name: str,
    label_prefixes: list[str],
    starting_class: str | None = None,
) -> dict[str, dict[str, int]]:
    """Trackable row rollups keyed by lower-cased row-label prefix."""
    prefixes: list[str] = []
    seen: set[str] = set()
    for raw in label_prefixes:
        norm = " ".join(str(raw or "").strip().lower().split())
        if not norm or norm in seen:
            continue
        seen.add(norm)
        prefixes.append(norm)
    if not prefixes:
        return {}

    eff, join, jparams = _state_clauses(starting_class)
    rows = conn.execute(
        f"""
        SELECT n.label, {eff} AS eff
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ?
          AND n.row_type IN ('checkbox', 'value')
        ORDER BY n.row_index
        """,
        (character_id, *jparams, run_id, sheet_name),
    ).fetchall()

    rollups = {prefix: _empty_roll() for prefix in prefixes}
    for row in rows:
        label_norm = " ".join(str(row["label"] or "").strip().lower().split())
        if not label_norm:
            continue
        matched_prefix: str | None = None
        for prefix in prefixes:
            if label_norm.startswith(f"{prefix} "):
                matched_prefix = prefix
                break
        if matched_prefix is None:
            continue

        roll = rollups[matched_prefix]
        roll["total"] += 1
        eff_state = str(row["eff"] or "todo")
        if eff_state == "done":
            roll["done"] += 1
        elif eff_state == "excluded":
            roll["excluded"] += 1

    for roll in rollups.values():
        roll["countable"] = roll["total"] - roll["excluded"]
    return rollups


def attach_content_virtual_nodes(
    conn: sqlite3.Connection,
    tree: list[dict],
    by_name: dict[str, dict],
    run_id: int,
    character_id: int,
    starting_class: str | None = None,
) -> None:
    """Attach synthetic child pages under content sheets.

    This powers sidebar-level subpages for sheets that contain multiple logical
    tracks (e.g., Mining/Quarrying) or repeated PAGE groups under expansion
    headers (e.g., Sightseeing Logs).
    """
    section_rows = conn.execute(
        """
        SELECT sheet_name, row_index, label, row_json
        FROM nodes
        WHERE run_id = ? AND row_type = 'section'
        ORDER BY sheet_name, row_index
        """,
        (run_id,),
    ).fetchall()

    sections_by_sheet: dict[str, list[dict]] = {}
    for row in section_rows:
        sheet_name = str(row["sheet_name"])
        payload = json.loads(row["row_json"] or "{}")
        meta = payload.get("section_sort") if isinstance(payload, dict) else None
        sections_by_sheet.setdefault(sheet_name, []).append(
            {
                "row_index": int(row["row_index"]),
                "label": str(row["label"] or ""),
                "meta": meta if isinstance(meta, dict) else None,
            }
        )

    def walk(node: dict) -> None:
        for child in node.get("children", []):
            walk(child)

        if node.get("is_menu") or node.get("is_virtual"):
            return

        sheet_name = str(node.get("sheet_name") or "")
        if not sheet_name:
            return
        sheet_meta = by_name.get(sheet_name)
        if sheet_meta is None:
            return

        sections = sections_by_sheet.get(sheet_name, [])
        specs = _content_virtual_specs_for_sheet(sheet_name, str(node.get("title") or ""), sections)
        if not specs and sheet_name == "Hunting Logs":
            hunting_prefixes = _hunting_log_label_prefixes(conn, run_id)
            if len(hunting_prefixes) >= 2:
                specs = [
                    {
                        "title": prefix,
                        "label_prefixes": [prefix],
                    }
                    for prefix in hunting_prefixes
                ]
        if not specs:
            return

        section_rollups = _section_trackable_rollups(
            conn,
            run_id,
            character_id,
            sheet_name,
            [int(s["row_index"]) for s in sections],
            starting_class,
        )

        used_names: set[str] = set(by_name.keys())
        virtual_children: list[dict] = []
        for ordinal, spec in enumerate(specs, start=1):
            section_indexes = [
                int(idx)
                for idx in spec.get("section_row_indexes", [])
                if int(idx) in section_rollups
            ]
            label_prefixes = [
                " ".join(str(p).split())
                for p in spec.get("label_prefixes", [])
                if str(p).strip()
            ]

            roll = _empty_roll()
            if section_indexes:
                for sec_idx in section_indexes:
                    sec_roll = section_rollups.get(sec_idx)
                    if sec_roll is None:
                        continue
                    roll["done"] += int(sec_roll.get("done") or 0)
                    roll["excluded"] += int(sec_roll.get("excluded") or 0)
                    roll["total"] += int(sec_roll.get("total") or 0)
                roll["countable"] = roll["total"] - roll["excluded"]
            elif label_prefixes:
                prefix_rollups = _label_prefix_trackable_rollups(
                    conn,
                    run_id,
                    character_id,
                    sheet_name,
                    label_prefixes,
                    starting_class,
                )
                for prefix in label_prefixes:
                    sec_roll = prefix_rollups.get(prefix.lower())
                    if sec_roll is None:
                        continue
                    roll["done"] += int(sec_roll.get("done") or 0)
                    roll["excluded"] += int(sec_roll.get("excluded") or 0)
                    roll["total"] += int(sec_roll.get("total") or 0)
                roll["countable"] = roll["total"] - roll["excluded"]
            else:
                continue

            if roll["total"] <= 0:
                continue

            title = str(spec.get("title") or "Group").strip() or "Group"
            slug = _slugify(title)
            base_name = f"{sheet_name}{VIRTUAL_SEP}{CONTENT_VIRTUAL_PREFIX}{slug}"
            v_name = base_name
            suffix = 2
            while v_name in used_names:
                v_name = f"{base_name}-{suffix}"
                suffix += 1
            used_names.add(v_name)

            virtual = {
                "sheet_name": v_name,
                "title": title,
                "is_menu": False,
                "is_readonly": False,
                "is_virtual": True,
                "virtual_kind": "content_group",
                "source_sheet": sheet_name,
                "section_row_indexes": section_indexes,
                "row_label_prefixes": label_prefixes,
                "parent_menu_section": None,
                "children": [],
                "roll": roll,
                "pct": pct(roll),
            }
            virtual_children.append(virtual)

            by_name[v_name] = {
                "sheet_name": v_name,
                "title": title,
                "is_menu": 0,
                "is_readonly": 0,
                "is_virtual": True,
                "virtual_kind": "content_group",
                "source_sheet": sheet_name,
                "section_row_indexes": section_indexes,
                "row_label_prefixes": label_prefixes,
                "parent_sheet": sheet_name,
                "parent_menu_section": None,
                "sheet_index": int(sheet_meta.get("sheet_index") or 0) * 1000 + ordinal,
                # Keep content-sheet metadata available for callers that only
                # have sheets_by_name.
                "data_columns_json": sheet_meta.get("data_columns_json", "[]"),
                "label_key": sheet_meta.get("label_key"),
                "value_key": sheet_meta.get("value_key"),
                "total_rows": roll["total"],
            }

        if virtual_children:
            node["children"] = [*node.get("children", []), *virtual_children]

    for root in tree:
        walk(root)


def _virtualize_sections(node: dict, by_name: dict[str, dict]) -> None:
    """Post-pass on a tree node: if its real children carry ``parent_menu_section``
    values, regroup them under synthetic intermediate "section" nodes so the
    sidebar / browse routes treat each section as its own clickable sub-menu.

    Real children whose section is None stay at the top level (rendered after
    the named groups, same as the menu page's "Other" bucket).

    Recurses into every child (real or virtual) so deeper hierarchies are
    virtualized too if the data ever supports them."""
    children = node.get("children") or []
    if not children:
        return

    section_groups: dict[str | None, list[dict]] = {}
    section_order: list[str | None] = []
    for child in children:
        sec = child.get("parent_menu_section")
        if sec not in section_groups:
            section_groups[sec] = []
            section_order.append(sec)
        section_groups[sec].append(child)

    # Nothing to virtualize: no named sections at all, OR a single named
    # section (which would just add a useless one-deep wrapper).
    named_section_count = sum(1 for s in section_order if s is not None)
    if named_section_count <= 1:
        for child in children:
            _virtualize_sections(child, by_name)
        return

    new_children: list[dict] = []
    for sec in section_order:
        bucket = section_groups[sec]
        if sec is None:
            # ungrouped real children stay direct children, rendered last
            for child in bucket:
                _virtualize_sections(child, by_name)
                new_children.append(child)
            continue

        v_name = f"{node['sheet_name']}{VIRTUAL_SEP}{sec}"
        v_roll = _empty_roll()
        for child in bucket:
            for k in v_roll:
                v_roll[k] += child["roll"][k]
            # Recurse so any deeper grouping is virtualized too.
            _virtualize_sections(child, by_name)

        virtual = {
            "sheet_name": v_name,
            "title": sec,
            "is_menu": True,
            "is_readonly": False,
            "is_virtual": True,
            "parent_menu_section": None,
            "children": bucket,
            "roll": v_roll,
            "pct": pct(v_roll),
        }
        new_children.append(virtual)

        # Register the virtual node in by_name so breadcrumbs / Ctx.sheets_by_name
        # lookups work the same as for real sheets. We also override the
        # children's parent_sheet so breadcrumb_path walks through the virtual.
        by_name[v_name] = {
            "sheet_name": v_name,
            "title": sec,
            "is_menu": 1,
            "is_readonly": 0,
            "is_virtual": True,
            "parent_sheet": node["sheet_name"],
            "parent_menu_section": None,
            "sheet_index": (
                min(by_name[c["sheet_name"]]["sheet_index"] for c in bucket
                    if c["sheet_name"] in by_name)
                if any(c["sheet_name"] in by_name for c in bucket) else 0
            ),
        }
        for c in bucket:
            if c["sheet_name"] in by_name:
                by_name[c["sheet_name"]] = {
                    **by_name[c["sheet_name"]],
                    "parent_sheet": v_name,
                }

    node["children"] = new_children


def build_nav_tree(
    sheets: list[sqlite3.Row], rollups: dict[str, dict[str, int]]
) -> tuple[list[dict], dict[str, int], dict[str, dict]]:
    """Return ``(roots, overall_rollup, by_name)``. ``by_name`` is a merged
    map of every node visible in the tree (real sheets + virtual section
    nodes) keyed by ``sheet_name``, suitable for breadcrumb walks and
    ``Ctx.sheets_by_name``. Each node in the tree carries an aggregated
    rollup; virtual section nodes carry ``is_virtual: True``."""
    by_name = {s["sheet_name"]: dict(s) for s in sheets}
    children: dict[str | None, list[str]] = {}
    for s in sheets:
        children.setdefault(s["parent_sheet"], []).append(s["sheet_name"])

    def build(name: str) -> dict:
        s = by_name[name]
        node = {
            "sheet_name": name,
            "title": s["title"],
            "is_menu": bool(s["is_menu"]),
            "is_readonly": bool(s["is_readonly"]),
            "is_virtual": False,
            # Surface the workbook's column-grouping for the menu page —
            # populated for content sheets whose parent menu used a
            # multi-column layout, NULL otherwise.
            "parent_menu_section": (
                s["parent_menu_section"]
                if "parent_menu_section" in s.keys() else None
            ),
            "children": [],
            "roll": _empty_roll(),
        }
        own = rollups.get(name) if not s["is_menu"] else None
        if own:
            for k in node["roll"]:
                node["roll"][k] += own[k]
        for child_name in children.get(name, []):
            child = build(child_name)
            node["children"].append(child)
            for k in node["roll"]:
                node["roll"][k] += child["roll"][k]
        node["children"].sort(key=lambda c: by_name[c["sheet_name"]]["sheet_index"])
        node["pct"] = pct(node["roll"])
        return node

    roots = [build(n) for n in children.get(None, [])]
    roots.sort(key=lambda c: by_name[c["sheet_name"]]["sheet_index"])

    # Post-pass: regroup any menu's children under virtual section nodes
    # if parent_menu_section was populated by ingest.
    for root in roots:
        _virtualize_sections(root, by_name)

    overall = _empty_roll()
    for r in roots:
        for k in overall:
            overall[k] += r["roll"][k]
    return roots, overall, by_name


def mark_active_path(tree: list[dict], active_sheet: str | None) -> None:
    """Flag every node that is, or contains, the active sheet (sidebar auto-open)."""
    def walk(node: dict) -> bool:
        hit = node["sheet_name"] == active_sheet
        for child in node["children"]:
            if walk(child):
                hit = True
        node["has_active"] = hit
        return hit

    for node in tree:
        walk(node)


def find_node(tree: list[dict], sheet_name: str) -> dict | None:
    for node in tree:
        if node["sheet_name"] == sheet_name:
            return node
        hit = find_node(node["children"], sheet_name)
        if hit:
            return hit
    return None


def breadcrumb_path(
    sheets_by_name: dict[str, dict], sheet_name: str
) -> list[dict]:
    """Walk parent_sheet up to the root; return ordered list of {sheet_name,title}."""
    chain: list[dict] = []
    cursor: str | None = sheet_name
    while cursor and cursor in sheets_by_name:
        s = sheets_by_name[cursor]
        chain.append({"sheet_name": cursor, "title": s["title"]})
        cursor = s["parent_sheet"]
    chain.reverse()
    return chain


def fetch_rows(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    sheet_name: str,
    q: str = "",
    state: str = "all",
    starting_class: str | None = None,
) -> list[dict]:
    """All nodes for a sheet with effective state + parsed data, in row order."""
    eff, join, jparams = _state_clauses(starting_class)
    params: list[Any] = [character_id, *jparams, run_id, sheet_name]
    where = ""
    if q.strip():
        # Keep section banners even when filtering so group boundaries remain
        # intact (virtual subgroup pages depend on section row_index anchors).
        where += " AND (n.row_type = 'section' OR n.row_json LIKE ?)"
        params.append(f"%{q.strip()}%")
    rows = conn.execute(
        f"""
        SELECT n.row_index, n.label, n.baseline_state, n.row_type,
               n.section_label, n.seq, n.row_json,
               {eff} AS eff,
               p.progress_percent
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ? {where}
        ORDER BY n.row_index
        """,
        params,
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(r["row_json"])
        d["is_section"] = r["row_type"] == "section"
        if r["row_type"] == "value":
            d["value_cap"] = resolve_value_cap(
                sheet_name,
                d.get("section_label"),
                d.get("label"),
            )
        if state != "all" and not d["is_section"] and d["eff"] != state:
            continue
        out.append(d)
    return out


def snapshot_trackable_rows(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    starting_class: str | None = None,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Snapshot effective state + progress for all trackable rows.

    Used by import history to compute before/after diffs without depending on
    UI filters or per-sheet traversal.
    """
    eff, join, jparams = _state_clauses(starting_class)
    rows = conn.execute(
        f"""
         SELECT n.sheet_name, n.row_index, n.row_type,
             p.state AS explicit_state,
               {eff} AS eff,
               p.progress_percent
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ?
          AND n.row_type IN ('checkbox', 'value')
        """,
        (character_id, *jparams, run_id),
    ).fetchall()

    snap: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        pct_raw = r["progress_percent"]
        pct = float(pct_raw) if isinstance(pct_raw, (int, float)) else None
        explicit_state_raw = r["explicit_state"]
        explicit_state = str(explicit_state_raw) if explicit_state_raw is not None else None
        key = (str(r["sheet_name"]), int(r["row_index"]))
        snap[key] = {
            "sheet_name": key[0],
            "row_index": key[1],
            "row_type": str(r["row_type"]),
            "state": str(r["eff"]),
            "progress_percent": pct,
            "explicit": explicit_state is not None,
            "explicit_state": explicit_state,
        }
    return snap


def sheet_supports_section_sort(sheet_name: str) -> bool:
    return section_sort.supports_sheet(sheet_name)


def group_rows_by_section(
    rows: list[dict],
    *,
    sheet_name: str | None = None,
    section_sort_mode: str = section_sort.SORT_MODE_WORKBOOK,
) -> list[dict]:
    """Turn a flat row list into [{section, rows:[...]}].

    By default the workbook row order is preserved. For supported sheets, a
    non-workbook ``section_sort_mode`` reorders section groups using stored
    metadata (or inferred metadata for older ingest runs).
    """
    groups: list[dict] = []
    current: dict | None = None
    for r in rows:
        if r["is_section"]:
            section_meta = None
            payload = r.get("data")
            if isinstance(payload, dict):
                raw_meta = payload.get("section_sort")
                if isinstance(raw_meta, dict):
                    section_meta = raw_meta
            section_name = r.get("section_label") or r.get("label")
            current = {
                "section": section_name,
                "row_index": r["row_index"],
                "rows": [],
                "section_sort": section_meta,
            }
            groups.append(current)
        else:
            if current is None:
                current = {
                    "section": None,
                    "row_index": 0,
                    "rows": [],
                    "section_sort": None,
                }
                groups.append(current)
            current["rows"].append(r)

    grouped = [g for g in groups if g["rows"]]
    if sheet_name is None:
        return grouped
    return section_sort.sort_group_dicts(sheet_name, grouped, section_sort_mode)


def sheet_chain_flags(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    sheet_name: str,
    starting_class: str | None = None,
) -> dict[int, dict]:
    """Lightweight per-row chain hints for a whole sheet (one pass, no N+1)."""
    eff, join, jparams = _state_clauses(starting_class)
    flags: dict[int, dict] = {}
    for r in conn.execute(
        f"""
        SELECT e.target_row_index AS ri, COUNT(*) AS prereqs,
               MAX(CASE WHEN {eff} != 'done' THEN 1 ELSE 0 END) AS blocked
        FROM edges e
        JOIN nodes n
          ON n.run_id = e.run_id AND n.sheet_name = e.sheet_name
         AND n.row_index = e.source_row_index
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = e.run_id
         AND p.sheet_name = e.sheet_name AND p.row_index = e.source_row_index
        {join}
        WHERE e.run_id = ? AND e.sheet_name = ? AND e.edge_type = 'sequence'
          AND e.source_row_index IS NOT NULL
        GROUP BY e.target_row_index
        """,
        (character_id, *jparams, run_id, sheet_name),
    ):
        flags.setdefault(r["ri"], {}).update(
            prereqs=r["prereqs"], blocked=bool(r["blocked"])
        )
    for r in conn.execute(
        """
        SELECT source_row_index AS ri, COUNT(*) AS unlocks
        FROM edges
        WHERE run_id = ? AND sheet_name = ? AND source_row_index IS NOT NULL
        GROUP BY source_row_index
        """,
        (run_id, sheet_name),
    ):
        flags.setdefault(r["ri"], {}).update(unlocks=r["unlocks"])
    for f in flags.values():
        f.setdefault("prereqs", 0)
        f.setdefault("unlocks", 0)
        f.setdefault("blocked", False)
    return flags


def fetch_row(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None = None,
) -> dict | None:
    eff, join, jparams = _state_clauses(starting_class)
    r = conn.execute(
        f"""
        SELECT n.row_index, n.label, n.row_type, n.section_label, n.row_json,
             {eff} AS eff, p.state AS explicit_state, p.progress_percent
        FROM nodes n
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.sheet_name = ? AND n.row_index = ?
        """,
        (character_id, *jparams, run_id, sheet_name, row_index),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(r["row_json"])
    if r["row_type"] == "value":
        d["value_cap"] = resolve_value_cap(
            sheet_name,
            d.get("section_label"),
            d.get("label"),
        )
    return d


# --- chains -----------------------------------------------------------------

def fetch_chain(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    sheet_name: str,
    row_index: int,
    starting_class: str | None = None,
) -> dict:
    """Prerequisite path (rows that come before via 'sequence' edges) plus
    what this row unlocks (sequence successors + explicit 'unlocks' edges)."""
    def state_of(idx: int) -> str:
        return effective_state(
            conn, character_id, run_id, sheet_name, idx, starting_class
        )

    def label_of(idx: int) -> str | None:
        r = conn.execute(
            "SELECT label FROM nodes WHERE run_id=? AND sheet_name=? AND row_index=?",
            (run_id, sheet_name, idx),
        ).fetchone()
        return r["label"] if r else None

    # walk backwards: collect the full prerequisite path
    prereqs: list[dict] = []
    seen = set()
    cursor = row_index
    while True:
        pr = conn.execute(
            """
            SELECT source_row_index FROM edges
            WHERE run_id=? AND sheet_name=? AND edge_type='sequence'
              AND target_row_index=? AND source_row_index IS NOT NULL
            """,
            (run_id, sheet_name, cursor),
        ).fetchone()
        if not pr or pr["source_row_index"] in seen:
            break
        idx = int(pr["source_row_index"])
        seen.add(idx)
        prereqs.append({"row_index": idx, "label": label_of(idx), "state": state_of(idx)})
        cursor = idx
    prereqs.reverse()

    # forward: immediate successors in the section + explicit unlocks
    unlocks: list[dict] = []
    for e in conn.execute(
        """
        SELECT edge_type, target_row_index, target_label FROM edges
        WHERE run_id=? AND sheet_name=? AND source_row_index=?
          AND edge_type IN ('sequence', 'unlocks')
        """,
        (run_id, sheet_name, row_index),
    ):
        idx = e["target_row_index"]
        unlocks.append({
            "row_index": idx,
            "label": e["target_label"] or (label_of(idx) if idx else None),
            "state": state_of(idx) if idx else None,
            "kind": e["edge_type"],
        })

    blocked = any(p["state"] != "done" for p in prereqs)
    return {"prereqs": prereqs, "unlocks": unlocks, "blocked": blocked}


def chain_sheets_overview(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    starting_class: str | None = None,
) -> list[dict]:
    """Sheets that carry meaningful prerequisite chains, with progress."""
    rows = conn.execute(
        """
        SELECT e.sheet_name, COUNT(*) AS links
        FROM edges e
        WHERE e.run_id = ? AND e.edge_type = 'sequence'
        GROUP BY e.sheet_name
        HAVING links >= 3
        ORDER BY links DESC
        """,
        (run_id,),
    ).fetchall()
    rollups = sheet_rollups(conn, run_id, character_id, starting_class)
    sheets_by_name = {
        s["sheet_name"]: s for s in conn.execute(
            "SELECT sheet_name, title FROM sheets WHERE run_id = ?", (run_id,)
        )
    }
    out = []
    for r in rows:
        roll = rollups.get(r["sheet_name"], _empty_roll())
        meta = sheets_by_name.get(r["sheet_name"])
        out.append({
            "sheet_name": r["sheet_name"],
            "title": meta["title"] if meta else r["sheet_name"],
            "links": r["links"],
            "roll": roll,
            "pct": pct(roll),
        })
    return out


# --- search -----------------------------------------------------------------

def search_nodes(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    q: str,
    limit: int = 80,
    starting_class: str | None = None,
) -> list[dict]:
    q = q.strip()
    if len(q) < 2:
        return []
    eff, join, jparams = _state_clauses(starting_class)
    rows = conn.execute(
        f"""
        SELECT n.sheet_name, n.row_index, n.label,
               {eff} AS eff,
               s.title AS sheet_title
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        {join}
        WHERE n.run_id = ? AND n.row_type != 'section' AND n.label LIKE ?
        ORDER BY n.label
        LIMIT ?
        """,
        (character_id, *jparams, run_id, f"%{q}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- export -----------------------------------------------------------------

def fetch_export_rows(
    conn: sqlite3.Connection,
    run_id: int,
    character_id: int,
    starting_class: str | None = None,
) -> list[sqlite3.Row]:
    eff, join, jparams = _state_clauses(starting_class)
    return list(
        conn.execute(
            f"""
            SELECT n.sheet_name, n.row_index, n.label, n.section_label,
                     n.row_type,
                     {eff} AS state,
                   p.progress_percent, n.row_json
            FROM nodes n
            LEFT JOIN character_progress p
              ON p.character_id = ? AND p.run_id = n.run_id
             AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
            {join}
            WHERE n.run_id = ? AND n.row_type != 'section'
            ORDER BY n.sheet_name, n.row_index
            """,
            (character_id, *jparams, run_id),
        )
    )
