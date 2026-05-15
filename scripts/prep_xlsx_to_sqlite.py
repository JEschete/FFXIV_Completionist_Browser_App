#!/usr/bin/env python3
"""Ingest the FFXIV completion workbook into SQLite for the web app.

Workbook-driven rules:
- Content-sheet parent is read from the "Main Page" hyperlink.
- Purple banner rows mark sections.
- Column A marker Y/N/X maps to done/todo/excluded.
- Unlock/require columns become explicit edges where possible.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

# --- workbook conventions ---------------------------------------------------

SECTION_FILL = "CC66FF"  # purple banner rows

TOP_MENUS = (
    "Character Menu",
    "Duty Menu",
    "Logs Menu",
    "Travel Menu",
    "Social Menu",
)

# submenu name prefix -> parent top menu
MENU_PREFIX_PARENT = {
    "Char. Menu - ": "Character Menu",
    "Duty Menu - ": "Duty Menu",
    "Logs Menu - ": "Logs Menu",
}

# data-column header keywords used to pick the "name" of a row
LABEL_KEYS = (
    "job",
    "class",
    "quest",
    "name",
    "title",
    "item",
    "achievement",
    "spell",
    "mount",
    "minion",
    "emote",
    "card",
    "orchestrion",
    "weapon",
    "tool",
    "barding",
    "fish",
    "log",
    "action",
    "voice",
    "currency",
    "building",
    "rank",
)

# headers that identify the numeric column on a per-row fillable sheet
VALUE_KEYS = ("current_level",)

# data-column header keywords that describe what a row unlocks / requires
UNLOCK_KEYS = ("unlock", "requires", "required", "prereq", "next")

# Sheets where each row carries its own user-entered numeric value (e.g. each
# job's current level). NOT rank ladders -- those are checkbox per tier.
VALUE_SHEET_PATTERNS = ("classes-jobs",)

# The 8 ARR starting classes the workbook's class-conditional formulas key on.
STARTING_CLASSES = (
    "ARCANIST",
    "ARCHER",
    "CONJURER",
    "GLADIATOR",
    "LANCER",
    "MARAUDER",
    "PUGILIST",
    "THAUMATURGE",
)

# The cell the workbook reads for the active starting class.
STARTING_CLASS_REF = ("Character Menu", "L2")

# Sheets that are reference/info only (not trackable content rows).
READONLY_SHEETS = {
    "Read Me",
    "Before You Get Started, Take A Moment To Look Over The Following Information.",
}


# --- helpers ----------------------------------------------------------------

def norm_key(value, fallback_index: int) -> str:
    text = "" if value is None else str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or f"col_{fallback_index}"


def norm_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    text = str(value).replace("\xa0", " ").strip()
    return text or None


def cell_fill(cell) -> str:
    try:
        rgb = cell.fill.start_color.rgb
        if isinstance(rgb, str) and rgb not in ("00000000", "FFFFFFFF"):
            return rgb[-6:].upper()
    except Exception:
        pass
    return ""


def parse_state(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    if v in ("y", "yes", "x_done", "done", "complete", "completed", "true"):
        return "done"
    if v in ("x", "excluded", "n/a", "na"):
        return "excluded"
    return "todo"


def link_target_sheet(cell) -> str | None:
    """Return the sheet name a hyperlink points at, or None."""
    hl = cell.hyperlink
    if hl is None:
        return None
    loc = hl.location or hl.target or ""
    m = re.match(r"^'?(.*?)'?!", str(loc))
    target = m.group(1) if m else str(loc)
    if not target or target.lower() == "null":
        return None
    return target


def split_candidates(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\s*(?:,|/|;|\||->|=>|\band\b)\s*", value, flags=re.IGNORECASE)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p.lower() not in {"x", "n", "y", "yes", "no", "-"}:
            out.append(p)
    return out


# --- structural analysis ----------------------------------------------------

def is_menu_sheet(name: str) -> bool:
    return name in TOP_MENUS or any(name.startswith(p) for p in MENU_PREFIX_PARENT)


def menu_parent(name: str) -> str | None:
    for prefix, parent in MENU_PREFIX_PARENT.items():
        if name.startswith(prefix):
            return parent
    return None


def find_parent_link(ws) -> str | None:
    """Content sheets carry a 'Main Page' hyperlink back to their parent menu."""
    for row in ws.iter_rows(min_row=1, max_row=3):
        for cell in row:
            if cell.hyperlink is None:
                continue
            label = str(cell.value or "").lower()
            target = link_target_sheet(cell)
            if target and ("main" in label or cell.column >= 6):
                return target
    return None


def detect_data_columns(ws) -> list[dict]:
    """Detect non-empty data headers from B onward until the sidebar link."""
    cols: list[dict] = []
    max_scan = min(max(ws.max_column or 0, 6), 14)
    for idx in range(2, max_scan + 1):
        cell = ws.cell(row=1, column=idx)
        if cell.hyperlink is not None:
            break
        text = norm_value(cell.value)
        if text is None:
            continue
        cols.append(
            {
                "index": idx,
                "letter": get_column_letter(idx),
                "key": norm_key(text, idx),
                "label": text,
            }
        )

    # de-dupe keys
    seen: dict[str, int] = {}
    for c in cols:
        key = c["key"]
        n = seen.get(key, 0)
        if n:
            c["key"] = f"{key}_{n + 1}"
        seen[c["key"]] = n + 1
    return cols


def pick_label_column(columns: list[dict], skip: list[dict] | None = None) -> dict | None:
    skip_keys = {c["key"] for c in (skip or []) if c}
    for key in LABEL_KEYS:
        for c in columns:
            if c["key"] in skip_keys:
                continue
            if key in c["key"]:
                return c
    for c in columns:
        if c["key"] not in skip_keys:
            return c
    return None


def pick_value_column(columns: list[dict]) -> dict | None:
    for key in VALUE_KEYS:
        for c in columns:
            if key in c["key"]:
                return c
    return None


def pick_unlock_column(columns: list[dict]) -> dict | None:
    for c in columns:
        if any(k in c["key"] for k in UNLOCK_KEYS):
            return c
    return None


# --- schema -----------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    sheet_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sheet_index INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    title TEXT NOT NULL,
    is_menu INTEGER NOT NULL DEFAULT 0,
    is_readonly INTEGER NOT NULL DEFAULT 0,
    parent_sheet TEXT,
    data_columns_json TEXT NOT NULL,
    label_key TEXT,
    value_key TEXT,
    total_rows INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    label TEXT,
    baseline_state TEXT NOT NULL DEFAULT 'todo',
    row_type TEXT NOT NULL DEFAULT 'checkbox',
    section_label TEXT,
    seq INTEGER NOT NULL DEFAULT 0,
    row_json TEXT NOT NULL
);

CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    source_row_index INTEGER,
    source_label TEXT,
    target_row_index INTEGER,
    target_label TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    starting_class TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE class_overrides (
    run_id         INTEGER NOT NULL,
    starting_class TEXT NOT NULL,
    sheet_name     TEXT NOT NULL,
    row_index      INTEGER NOT NULL,
    state          TEXT NOT NULL CHECK(state IN ('done','todo','excluded')),
    PRIMARY KEY (run_id, starting_class, sheet_name, row_index)
);

CREATE INDEX idx_class_overrides_lookup
    ON class_overrides(run_id, starting_class, sheet_name);

CREATE TABLE character_progress (
    character_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('done','todo','excluded')),
    progress_percent REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (character_id, run_id, sheet_name, row_index)
);

CREATE TABLE progress_rollup (
    character_id INTEGER NOT NULL,
    run_id       INTEGER NOT NULL,
    sheet_name   TEXT NOT NULL,
    done         INTEGER NOT NULL DEFAULT 0,
    excluded     INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (character_id, run_id, sheet_name)
);

CREATE INDEX idx_nodes_sheet ON nodes(run_id, sheet_name, row_index);
CREATE INDEX idx_edges_sheet ON edges(run_id, sheet_name, edge_type);
CREATE INDEX idx_progress ON character_progress(character_id, run_id, sheet_name);
"""


# --- starting-class formula evaluation --------------------------------------

class FormulaEvaluator:
    """Tiny Excel formula interpreter for class-conditional IF/equality/cell refs."""

    def __init__(self, wb, starting_class: str):
        self.wb = wb
        self.starting_class = starting_class
        self._cache: dict[tuple[str, str], object] = {}

    def cell_value(self, sheet_name: str, coord: str):
        key = (sheet_name, coord)
        if key in self._cache:
            return self._cache[key]
        if (sheet_name, coord) == STARTING_CLASS_REF:
            self._cache[key] = self.starting_class
            return self.starting_class
        try:
            cell = self.wb[sheet_name][coord]
        except (KeyError, ValueError):
            return None
        v = cell.value
        if isinstance(v, str) and v.startswith("="):
            try:
                v = self.eval_formula(v, sheet_name)
            except Exception:
                v = None
        self._cache[key] = v
        return v

    def eval_formula(self, formula: str, current_sheet: str):
        return self._eval_expr(formula[1:].strip(), current_sheet)

    def _eval_expr(self, expr: str, sheet: str):
        expr = expr.strip()
        if not expr:
            return None

        u = expr.upper()
        if u.startswith("IF(") and expr.endswith(")"):
            args = self._split_args(expr[3:-1])
            if len(args) != 3:
                return None
            cond = self._eval_expr(args[0], sheet)
            return self._eval_expr(args[1] if cond else args[2], sheet)

        lhs, rhs = self._split_compare(expr)
        if lhs is not None and rhs is not None:
            return self._eval_expr(lhs, sheet) == self._eval_expr(rhs, sheet)

        if expr.startswith('"') and expr.endswith('"'):
            return expr[1:-1]
        if u == "TRUE":
            return True
        if u == "FALSE":
            return False

        m = re.match(r"^'([^']+)'!\$?([A-Z]+)\$?(\d+)$", expr)
        if m:
            return self.cell_value(m.group(1), m.group(2) + m.group(3))

        m = re.match(r"^\$?([A-Z]+)\$?(\d+)$", expr)
        if m:
            return self.cell_value(sheet, m.group(1) + m.group(2))

        try:
            return float(expr)
        except ValueError:
            return expr

    @staticmethod
    def _split_args(body: str) -> list[str]:
        depth = 0
        in_quote = False
        cur: list[str] = []
        out: list[str] = []
        for ch in body:
            if ch == '"':
                in_quote = not in_quote
                cur.append(ch)
            elif in_quote:
                cur.append(ch)
            elif ch == "(":
                depth += 1
                cur.append(ch)
            elif ch == ")":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                out.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            out.append("".join(cur).strip())
        return out

    @staticmethod
    def _split_compare(expr: str) -> tuple[str | None, str | None]:
        depth = 0
        in_quote = False
        for i, ch in enumerate(expr):
            if ch == '"':
                in_quote = not in_quote
            elif in_quote:
                continue
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "=" and depth == 0:
                return expr[:i].strip(), expr[i + 1 :].strip()
        return None, None


def _flatten_refs(wb, sheet_name: str, coord: str, depth: int) -> str:
    """Concatenate formula text reachable from a cell within a few hops."""
    if depth <= 0:
        return ""
    try:
        v = wb[sheet_name][coord].value
    except (KeyError, ValueError):
        return ""
    if not isinstance(v, str) or not v.startswith("="):
        return ""

    out = [v]
    for sn, ref in re.findall(r"'([^']+)'!\$?([A-Z]+\d+)", v):
        out.append(_flatten_refs(wb, sn, ref, depth - 1))
    for ref in re.findall(r"(?<!')\b([A-Z]+\d+)\b", v):
        out.append(_flatten_refs(wb, sheet_name, ref, depth - 1))
    return " ".join(out)


def collect_class_overrides(
    xlsx_path: Path, run_id: int
) -> list[tuple[int, str, str, int, str]]:
    """Evaluate class-dependent formula cells and emit per-class state overrides."""
    wb = load_workbook(filename=xlsx_path, data_only=False)

    formula_cells: list[tuple[str, int]] = []
    for name in wb.sheetnames:
        if is_menu_sheet(name) or name in READONLY_SHEETS:
            continue
        ws = wb[name]
        if ws.sheet_state != "visible":
            continue
        for row in ws.iter_rows(min_row=2, max_row=min(ws.max_row or 0, 2000)):
            if not row:
                continue
            a = row[0]
            v = a.value
            if not (isinstance(v, str) and v.startswith("=")):
                continue
            if "L2" not in _flatten_refs(wb, name, a.coordinate, depth=3):
                continue
            formula_cells.append((name, int(a.row or 0)))

    per_class: dict[str, dict[tuple[str, int], str]] = {}
    for cls in STARTING_CLASSES:
        ev = FormulaEvaluator(wb, cls)
        per_class[cls] = {}
        for sheet_name, row_idx in formula_cells:
            coord = f"A{row_idx}"
            val = ev.cell_value(sheet_name, coord)
            if val in ("X", "N", "Y"):
                state = {"X": "excluded", "N": "todo", "Y": "done"}[val]
                per_class[cls][(sheet_name, row_idx)] = state

    rows: list[tuple[int, str, str, int, str]] = []
    all_cells = {k for d in per_class.values() for k in d}
    for key in all_cells:
        results = {cls: per_class[cls].get(key) for cls in STARTING_CLASSES}
        if len(set(results.values())) <= 1:
            continue
        sheet_name, row_idx = key
        for cls, state in results.items():
            if state is None:
                continue
            rows.append((run_id, cls, sheet_name, row_idx, state))
    return rows


# --- schema rebuild ---------------------------------------------------------

def rebuild_schema(conn: sqlite3.Connection) -> tuple[list[tuple], list[tuple]]:
    """Drop data tables and return saved characters/progress for migration."""
    saved_chars: list[tuple] = []
    saved_progress: list[tuple] = []

    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "characters" in existing:
        # Try the newer 4-column form first; fall back if the existing DB
        # predates the starting_class column. The reinsert below pads 3-tuples
        # to 4 by inserting NULL for starting_class.
        try:
            saved_chars = list(conn.execute(
                "SELECT id, name, starting_class, created_at FROM characters"
            ))
        except sqlite3.OperationalError:
            saved_chars = list(conn.execute(
                "SELECT id, name, created_at FROM characters"
            ))
    if "character_progress" in existing:
        try:
            saved_progress = list(
                conn.execute(
                    """
                    SELECT character_id, sheet_name, row_index, state, progress_percent, updated_at
                    FROM character_progress
                    WHERE run_id = (SELECT MAX(run_id) FROM character_progress)
                    """
                )
            )
        except sqlite3.OperationalError:
            saved_progress = []

    for table in (
        "edges",
        "nodes",
        "sheet_rows",
        "sheet_cells",
        "chain_edges",
        "chain_nodes",
        "sheets",
        "ingest_runs",
        "progress_rollup",
        "class_overrides",
        "character_progress",
        "characters",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")

    conn.executescript(SCHEMA)
    conn.commit()
    return saved_chars, saved_progress


# --- ingest -----------------------------------------------------------------

def ingest(xlsx_path: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)

    saved_chars, saved_progress = rebuild_schema(conn)

    started = dt.datetime.now().isoformat(timespec="seconds")
    raw_run_id = conn.execute(
        "INSERT INTO ingest_runs (source_file, started_at) VALUES (?, ?)",
        (str(xlsx_path), started),
    ).lastrowid
    if raw_run_id is None:
        raise RuntimeError("Failed to insert ingest_runs row")
    run_id = int(raw_run_id)

    print(f"Loading workbook {xlsx_path} ...")
    wb = load_workbook(filename=xlsx_path, data_only=True)
    sheet_names = set(wb.sheetnames)

    total_rows = 0
    for sheet_index, sheet_name in enumerate(wb.sheetnames, start=1):
        ws = wb[sheet_name]
        is_menu = is_menu_sheet(sheet_name)
        is_readonly = sheet_name in READONLY_SHEETS

        if is_menu:
            parent = menu_parent(sheet_name)
        elif is_readonly:
            parent = None
        else:
            parent = find_parent_link(ws)
            if parent and parent not in sheet_names:
                parent = None

        columns = detect_data_columns(ws)
        is_value_sheet = any(p in sheet_name.lower() for p in VALUE_SHEET_PATTERNS)
        value_col = pick_value_column(columns) if is_value_sheet else None
        label_col = pick_label_column(columns, skip=[value_col] if value_col else None)
        unlock_col = pick_unlock_column(columns)
        label_key = label_col["key"] if label_col else None
        value_key = value_col["key"] if value_col else None

        if is_menu or is_readonly:
            title = sheet_name
            for row in ws.iter_rows(min_row=2, max_row=2):
                banner = norm_value(row[0].value)
                if banner:
                    title = banner.title()
                break
            conn.execute(
                """
                INSERT INTO sheets (
                    run_id, sheet_index, sheet_name, title,
                    is_menu, is_readonly, parent_sheet, data_columns_json,
                    label_key, value_key, total_rows
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sheet_index,
                    sheet_name,
                    title,
                    int(is_menu),
                    int(is_readonly),
                    parent,
                    json.dumps(columns),
                    label_key,
                    value_key,
                    0,
                ),
            )
            print(f"  [{sheet_index:3}] {sheet_name:<52} menu/readonly")
            continue

        node_rows: list[tuple] = []
        seq_edges: list[tuple] = []
        unlock_edges: list[tuple] = []
        label_to_row: dict[str, int] = {}
        current_section: str | None = None
        seq_in_section = 0
        prev_track_row: int | None = None
        sheet_title = sheet_name

        for r_idx, row in enumerate(ws.iter_rows(), start=1):
            if r_idx == 1:
                continue

            a_cell = row[0] if row else None
            a_val = norm_value(a_cell.value) if a_cell else None
            a_fill = cell_fill(a_cell) if a_cell else ""

            data: dict[str, str] = {}
            for c in columns:
                cell = ws.cell(row=r_idx, column=c["index"])
                v = norm_value(cell.value)
                if v is not None:
                    data[c["key"]] = v

            is_banner = a_fill == SECTION_FILL or (
                a_val is not None and not data and a_val.isupper() and len(a_val) >= 4
            )

            if is_banner:
                if r_idx == 2:
                    sheet_title = (a_val or sheet_name).title()
                current_section = (a_val or "").title() or sheet_name
                seq_in_section = 0
                prev_track_row = None
                node_rows.append(
                    (
                        run_id,
                        sheet_name,
                        r_idx,
                        a_val,
                        "todo",
                        "section",
                        current_section,
                        0,
                        json.dumps(data),
                    )
                )
                continue

            if not data and a_val is None:
                continue

            label = data.get(label_key) if label_key else None
            if not label:
                label = next(iter(data.values()), None)

            state = parse_state(a_val)
            if a_val is None and not is_value_sheet:
                state = "todo"

            row_type = "value" if is_value_sheet else "checkbox"

            node_rows.append(
                (
                    run_id,
                    sheet_name,
                    r_idx,
                    label,
                    state,
                    row_type,
                    current_section,
                    seq_in_section,
                    json.dumps(data),
                )
            )
            if label:
                label_to_row.setdefault(label.strip().lower(), r_idx)

            if row_type == "checkbox" and prev_track_row is not None:
                seq_edges.append(
                    (
                        run_id,
                        sheet_name,
                        "sequence",
                        prev_track_row,
                        None,
                        r_idx,
                        label,
                        1,
                    )
                )
            prev_track_row = r_idx
            seq_in_section += 1

            if unlock_col and unlock_col["key"] in data:
                for cand in split_candidates(data[unlock_col["key"]]):
                    unlock_edges.append(
                        (
                            run_id,
                            sheet_name,
                            "unlocks",
                            r_idx,
                            label,
                            None,
                            cand,
                            0,
                        )
                    )

        resolved_unlocks = []
        for e in unlock_edges:
            tgt = label_to_row.get((e[6] or "").strip().lower())
            resolved_unlocks.append(e[:5] + (tgt, e[6], 1 if tgt else 0))

        track_total = sum(1 for n in node_rows if n[5] in ("checkbox", "value"))

        conn.execute(
            """
            INSERT INTO sheets (
                run_id, sheet_index, sheet_name, title,
                is_menu, is_readonly, parent_sheet, data_columns_json,
                label_key, value_key, total_rows
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sheet_index,
                sheet_name,
                sheet_title,
                0,
                0,
                parent,
                json.dumps(columns),
                label_key,
                value_key,
                track_total,
            ),
        )

        conn.executemany(
            """
            INSERT INTO nodes (
                run_id, sheet_name, row_index, label,
                baseline_state, row_type, section_label, seq, row_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            node_rows,
        )

        conn.executemany(
            """
            INSERT INTO edges (
                run_id, sheet_name, edge_type,
                source_row_index, source_label, target_row_index,
                target_label, resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            seq_edges + resolved_unlocks,
        )

        total_rows += track_total
        print(
            f"  [{sheet_index:3}] {sheet_name:<52} rows={track_total:<5} "
            f"edges={len(seq_edges) + len(resolved_unlocks)} parent={parent}"
        )

    if saved_chars:
        conn.executemany(
            "INSERT INTO characters (id, name, starting_class, created_at) VALUES (?, ?, ?, ?)",
            [(c[0], c[1], None, c[2]) if len(c) == 3 else c for c in saved_chars],
        )
    else:
        conn.execute(
            "INSERT INTO characters (name, starting_class, created_at) VALUES (?, ?, ?)",
            ("Adventurer", None, started),
        )

    if saved_progress:
        conn.executemany(
            """
            INSERT OR IGNORE INTO character_progress
                (character_id, run_id, sheet_name, row_index, state, progress_percent, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(p[0], run_id, p[1], p[2], p[3], p[4], p[5]) for p in saved_progress],
        )
        print(f"\nMigrated {len(saved_progress)} progress rows to run {run_id}")

    print("\nScanning class-conditional formulas (second workbook pass)...")
    overrides = collect_class_overrides(xlsx_path, run_id)
    if overrides:
        conn.executemany(
            """
            INSERT INTO class_overrides
                (run_id, starting_class, sheet_name, row_index, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            overrides,
        )
        affected_cells = len({(s, r) for _, _, s, r, _ in overrides})
        per_sheet: dict[str, int] = {}
        for _, _, s, _, _ in overrides:
            per_sheet[s] = per_sheet.get(s, 0) + 1
        print(f"  class_overrides: {len(overrides)} rows across {affected_cells} cells")
        for s, n in sorted(per_sheet.items(), key=lambda x: -x[1]):
            print(f"    {s:<52} {n}")

    completed = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE ingest_runs SET completed_at = ?, sheet_count = ?, row_count = ? WHERE id = ?",
        (completed, len(wb.sheetnames), total_rows, run_id),
    )

    conn.commit()
    conn.close()

    print(f"\nIngest complete -> run {run_id}")
    print(f"  sheets : {len(wb.sheetnames)}")
    print(f"  rows   : {total_rows}")


def resolve_xlsx(explicit: Path | None) -> Path:
    if explicit:
        return explicit

    # Default source is the Spreadsheet folder, regardless of workbook filename.
    spreadsheet_dir: Path | None = None
    for child in Path.cwd().iterdir():
        if child.is_dir() and child.name.lower() == "spreadsheet":
            spreadsheet_dir = child
            break

    if spreadsheet_dir is None:
        raise SystemExit("Spreadsheet folder not found in project root.")

    candidates = sorted(
        (p for p in spreadsheet_dir.glob("*.xlsx") if not p.name.startswith("~$")),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        raise SystemExit(f"No .xlsx found in {spreadsheet_dir}; pass --xlsx <path>.")
    print(f"Auto-selected workbook: {candidates[-1]}")
    return candidates[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest the FFXIV workbook into SQLite.")
    parser.add_argument(
        "--xlsx",
        type=Path,
        help="source workbook override (default: newest .xlsx in Spreadsheet folder)",
    )
    parser.add_argument("--db", type=Path, default=Path("data/ffxiv_tracker.sqlite"))
    args = parser.parse_args()

    ingest(resolve_xlsx(args.xlsx), args.db)


if __name__ == "__main__":
    main()
