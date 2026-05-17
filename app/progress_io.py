"""Per-character progress sidecars: durable, portable, workbook-agnostic.

The SQLite ``character_progress`` table is treated as a working index. The
**source of truth** for each character's progress is a JSON file under
``data/progress/<CharacterName>.json``. On startup, ``reconcile_all`` rebuilds
the DB from those sidecars; on every state change, ``record_state_change``
writes through to both the DB (for fast reads) and the sidecar (for durability).

Each progress entry carries a *tiered identity*: four possible ways to
re-locate the same workbook row even after the rows shift around. From
strongest to weakest:

  1. ``sheet:Foo|section:Bar|label:Quest Name``  — most specific
  2. ``sheet:Foo|label:Quest Name``              — when label is unique on sheet
  3. ``sheet:Foo|hash:7c4f3a8b9e1d``             — content fingerprint (12 hex)
  4. ``sheet:Foo|row:N``                          — absolute fallback

All four are stored on every entry. The reconciler walks them top-to-bottom
when re-resolving an entry to a current node, so a row that shifted position
or got renamed in one dimension can still be found via the others. Entries
that fail every tier stay in the file as ``orphan`` so they re-resolve
automatically if the row reappears in a future workbook revision.

This module is deliberately self-contained — ``app/main.py`` only needs to
call ``reconcile_all`` once on startup, and ``app/db.py`` only needs to call
``record_state_change`` after a state-write.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROGRESS_DIR = ROOT / "data" / "progress"
SCHEMA_VERSION = "ffxiv-tracker/v1"

# Safety-first default: do NOT allow sheet+row position fallback unless
# explicitly enabled (e.g. for aggressive one-off recovery).
_ALLOW_POSITION_FALLBACK_ENV = os.getenv(
    "FFXIV_PROGRESS_ALLOW_POSITION_FALLBACK", ""
).strip().lower()
ALLOW_POSITION_FALLBACK_DEFAULT = _ALLOW_POSITION_FALLBACK_ENV in {
    "1", "true", "yes", "on"
}


# --- identity computation --------------------------------------------------

def _hash_row(row_json: str) -> str:
    """12-hex-char SHA-256 of a row's JSON. Stable as long as the column
    values don't change; shifts independently of row position or label."""
    if not row_json:
        return ""
    # normalize: parse + re-dump with sorted keys so whitespace / key order
    # in the source JSON doesn't perturb the hash
    try:
        normalized = json.dumps(json.loads(row_json), sort_keys=True,
                                ensure_ascii=True)
    except json.JSONDecodeError:
        normalized = row_json
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def compute_stable_ids(
    sheet_name: str,
    section_label: str | None,
    label: str | None,
    row_json: str | None,
    row_index: int,
    *,
    precomputed_hash: str | None = None,
) -> dict[str, str]:
    """Build the four-tier identity dict for one workbook row.

    Tiers that lack the data they need (e.g. label is None) are omitted, so
    callers can always treat ``ids`` as "all the keys we *could* compute"."""
    out: dict[str, str] = {}
    if section_label and label:
        out["section_label"] = (
            f"sheet:{sheet_name}|section:{section_label}|label:{label}"
        )
    if label:
        out["label"] = f"sheet:{sheet_name}|label:{label}"
    h = precomputed_hash or (_hash_row(row_json) if row_json else "")
    if h:
        out["hash"] = f"sheet:{sheet_name}|hash:{h}"
    out["position"] = f"sheet:{sheet_name}|row:{row_index}"
    return out


# --- sidecar I/O -----------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sidecar_path(character_name: str) -> Path:
    """Return the JSON path for a character. Filename is the character name
    with non-portable characters replaced; collisions across slightly-
    differently-spelled names would be rare and surface immediately."""
    safe = _SAFE_NAME_RE.sub("_", character_name.strip()) or "_unnamed"
    return PROGRESS_DIR / f"{safe}.json"


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _new_doc(character: dict, entries: list | None = None) -> dict:
    """Fresh sidecar document for a character (no entries unless provided)."""
    return {
        "schema": SCHEMA_VERSION,
        "character": {
            "name": character.get("name") or character.get("character_name"),
            "starting_class": character.get("starting_class"),
            "created_at": character.get("created_at") or _now_iso(),
        },
        "progress": list(entries or []),
    }


def load_sidecar(path: Path) -> dict | None:
    """Read a sidecar file. Returns None if missing or unreadable; logs to
    a sibling ``.bak`` and returns None on JSON corruption (so the next save
    overwrites cleanly without silently losing the broken file)."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        bak = path.with_suffix(path.suffix + ".corrupt")
        try:
            path.replace(bak)
        except OSError:
            pass
        return None
    if not isinstance(doc, dict) or "progress" not in doc:
        return None
    return doc


def save_sidecar(path: Path, doc: dict) -> None:
    """Atomic write: dump to a sibling temp file, fsync, replace. Eliminates
    the half-written-file failure mode on a crash mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_sidecars() -> list[Path]:
    if not PROGRESS_DIR.exists():
        return []
    return sorted(PROGRESS_DIR.glob("*.json"))


# --- in-memory doc cache + write batching ---------------------------------
#
# Toggling a row on a near-100% character used to take ~280 ms because every
# `set_row_state` reloaded and rewrote the ~13 MB sidecar from scratch — and a
# chain cascade multiplied that by the number of rows in the chain. We now
# keep the parsed doc per character in-memory; the disk save only happens at
# the end of an explicit ``batch(...)`` block (one per cascade) or, for a
# lone single-row toggle, immediately after the in-memory mutation.
#
# Cache is process-wide. The reconciler at startup runs before any toggles,
# so we don't have to worry about it racing with cached state. Per-path
# locks serialize concurrent toggles on the same character (FastAPI sync
# endpoints run in a threadpool, so this can happen).

_cache_lock = threading.Lock()
_doc_cache: dict[Path, dict] = {}
_path_locks: dict[Path, threading.RLock] = {}
_batch_depth: dict[Path, int] = {}
_dirty: set[Path] = set()


def _path_lock(path: Path) -> threading.RLock:
    with _cache_lock:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _path_locks[path] = lock
        return lock


def _get_doc(path: Path, fresh_factory) -> dict:
    """Return the cached parsed sidecar, loading from disk on first access.
    ``fresh_factory`` builds a new doc when the file doesn't exist yet."""
    doc = _doc_cache.get(path)
    if doc is None:
        doc = load_sidecar(path) or fresh_factory()
        _doc_cache[path] = doc
    return doc


def _flush(path: Path) -> None:
    """Write the cached doc for ``path`` to disk if it's dirty."""
    if path not in _dirty:
        return
    doc = _doc_cache.get(path)
    if doc is None:
        _dirty.discard(path)
        return
    save_sidecar(path, doc)
    _dirty.discard(path)


def invalidate_cache(path: Path | None = None) -> None:
    """Drop in-memory cached docs. Pass ``None`` to clear everything (used by
    the reconciler after it rewrites the on-disk state). Any dirty buffer is
    flushed first so we don't lose pending writes."""
    with _cache_lock:
        paths = [path] if path is not None else list(_doc_cache.keys())

    # Important: do not call _path_lock while already holding _cache_lock.
    # _path_lock itself acquires _cache_lock, which would deadlock.
    for p in paths:
        lock = _path_lock(p)
        with lock:
            _flush(p)
            with _cache_lock:
                _doc_cache.pop(p, None)


@contextmanager
def batch(conn: sqlite3.Connection, character_id: int):
    """Defer sidecar disk writes until the outermost batch exits.

    Wraps a multi-row write (chain cascade) so the 13 MB sidecar gets a single
    save at the end instead of one per cascaded row. Nests safely. If
    ``character_id`` can't be resolved to a sidecar, this is a no-op so the
    caller doesn't need a separate code path."""
    char = conn.execute(
        "SELECT name FROM characters WHERE id = ?", (character_id,)
    ).fetchone()
    if not char or not char["name"]:
        yield
        return
    path = sidecar_path(char["name"])
    lock = _path_lock(path)
    with lock:
        _batch_depth[path] = _batch_depth.get(path, 0) + 1
    try:
        yield
    finally:
        with lock:
            depth = _batch_depth.get(path, 1) - 1
            if depth <= 0:
                _batch_depth.pop(path, None)
                _flush(path)
            else:
                _batch_depth[path] = depth


# --- write-through from set_row_state --------------------------------------

def _node_for_row(
    conn: sqlite3.Connection, run_id: int, sheet_name: str, row_index: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT label, section_label, row_json,
               COALESCE(stable_hash, '') AS stable_hash
        FROM nodes
        WHERE run_id = ? AND sheet_name = ? AND row_index = ?
        """,
        (run_id, sheet_name, row_index),
    ).fetchone()


def record_state_change(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
    state: str,
    progress_percent: float | None = None,
) -> None:
    """Write through a state change to the character's JSON sidecar.

    Looked up from the DB (cheap): the character name, and the node's label /
    section / hash. If either lookup fails the call no-ops — the DB write
    still succeeded, we just don't have enough metadata to checkpoint."""
    char = conn.execute(
        "SELECT name, starting_class, created_at FROM characters WHERE id = ?",
        (character_id,),
    ).fetchone()
    if not char or not char["name"]:
        return
    node = _node_for_row(conn, run_id, sheet_name, row_index)
    if not node:
        return

    ids = compute_stable_ids(
        sheet_name, node["section_label"], node["label"],
        node["row_json"], row_index,
        precomputed_hash=node["stable_hash"] or None,
    )
    path = sidecar_path(char["name"])

    new_entry: dict = {
        "ids": ids,
        "state": state,
        "ts": _now_iso(),
    }
    if progress_percent is not None:
        new_entry["value"] = progress_percent

    # Serialize concurrent writers on the same sidecar. Under a chain cascade
    # the same thread reenters this for ~N rows; the outer batch() holds the
    # RLock for the whole cascade so contention is rare in practice.
    char_dict = dict(char)
    with _path_lock(path):
        doc = _get_doc(path, lambda: _new_doc(char_dict))

        # find an existing entry that matches by ANY of the new entry's ids
        match_idx: int | None = None
        new_id_values = set(ids.values())
        for i, e in enumerate(doc["progress"]):
            if not isinstance(e, dict):
                continue
            e_ids = (e.get("ids") or {}).values()
            if any(v in new_id_values for v in e_ids):
                match_idx = i
                break

        if match_idx is None:
            doc["progress"].append(new_entry)
        else:
            # preserve any unrelated fields the user might have added
            prev = doc["progress"][match_idx]
            prev.update(new_entry)
            prev.pop("orphan", None)

        _dirty.add(path)
        if _batch_depth.get(path, 0) == 0:
            _flush(path)


# --- reconcile JSON sidecars -> DB at startup ------------------------------

@dataclass
class CharacterReport:
    name: str
    matched_section_label: int = 0
    matched_label: int = 0
    matched_hash: int = 0
    matched_position: int = 0
    orphaned: int = 0
    bootstrapped_from_db: int = 0


@dataclass
class ReconcileReport:
    characters: list[CharacterReport] = field(default_factory=list)

    def total_matched(self) -> int:
        return sum(
            c.matched_section_label + c.matched_label + c.matched_hash
            + c.matched_position for c in self.characters
        )

    def total_orphaned(self) -> int:
        return sum(c.orphaned for c in self.characters)

    def summary(self) -> str:
        if not self.characters:
            return "no character sidecars found"
        rows = [
            f"  {c.name:<24} matched {c.matched_section_label}/{c.matched_label}"
            f"/{c.matched_hash}/{c.matched_position} (sec/lbl/hash/pos)"
            f"   orphaned {c.orphaned}"
            + (f"   bootstrapped {c.bootstrapped_from_db}"
               if c.bootstrapped_from_db else "")
            for c in self.characters
        ]
        return "\n".join(rows)


def _ensure_character(
    conn: sqlite3.Connection, name: str, header: dict
) -> int:
    """Find or create a character by name; return its id."""
    row = conn.execute(
        "SELECT id FROM characters WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO characters (name, starting_class, created_at) "
        "VALUES (?, ?, ?)",
        (name, header.get("starting_class"),
         header.get("created_at") or _now_iso()),
    )
    return int(cur.lastrowid)


def _bootstrap_from_db(
    conn: sqlite3.Connection, character: sqlite3.Row, run_id: int
) -> int:
    """One-time export: DB progress for this character → JSON sidecar.
    Returns the entry count written. Idempotent only by sidecar existence —
    callers must check before calling so we don't overwrite curated JSON."""
    rows = conn.execute(
        """
        SELECT p.sheet_name, p.row_index, p.state, p.progress_percent,
               p.updated_at, n.label, n.section_label, n.row_json,
               COALESCE(n.stable_hash, '') AS stable_hash
        FROM character_progress p
        LEFT JOIN nodes n
          ON n.run_id = p.run_id AND n.sheet_name = p.sheet_name
         AND n.row_index = p.row_index
        WHERE p.character_id = ? AND p.run_id = ?
        """,
        (character["id"], run_id),
    ).fetchall()
    if not rows:
        return 0
    entries: list[dict] = []
    for r in rows:
        ids = compute_stable_ids(
            r["sheet_name"], r["section_label"], r["label"],
            r["row_json"], int(r["row_index"]),
            precomputed_hash=r["stable_hash"] or None,
        )
        e: dict = {"ids": ids, "state": r["state"],
                   "ts": r["updated_at"] or _now_iso()}
        if r["progress_percent"] is not None:
            e["value"] = float(r["progress_percent"])
        entries.append(e)
    save_sidecar(sidecar_path(character["name"]),
                 _new_doc(dict(character), entries))
    return len(entries)


_NodeIndex = dict[str, dict]


def _build_node_index(conn: sqlite3.Connection, run_id: int) -> _NodeIndex:
    """Snapshot every node for the run into four dict-keyed indexes (one per
    identity tier). Replaces ``N×4`` per-entry tier SELECTs with a single
    bulk scan + O(1) Python lookups — the difference between ~15 s and
    ~0.2 s when replaying a 100 %-completion sidecar.

    Each index value is a small dict carrying everything ``compute_stable_ids``
    needs, so we never have to round-trip back to the DB just to refresh a
    weakly-matched entry's tiered identity.
    """
    rows = conn.execute(
        """SELECT sheet_name, row_index, label, section_label, row_json,
                  COALESCE(stable_hash, '') AS stable_hash
           FROM nodes WHERE run_id = ?""",
        (run_id,),
    ).fetchall()
    by_section_label: dict = {}
    by_label: dict = {}
    by_hash: dict = {}
    by_position: dict = {}
    for r in rows:
        ref = {
            "sheet_name": r["sheet_name"],
            "row_index": int(r["row_index"]),
            "label": r["label"],
            "section_label": r["section_label"],
            "row_json": r["row_json"],
            "stable_hash": r["stable_hash"] or None,
        }
        sn_lc = ref["sheet_name"].lower()
        if ref["section_label"] and ref["label"]:
            by_section_label.setdefault(
                (sn_lc, ref["section_label"].lower(), ref["label"].lower()), ref
            )
        if ref["label"]:
            by_label.setdefault((sn_lc, ref["label"].lower()), ref)
        if ref["stable_hash"]:
            by_hash.setdefault((sn_lc, ref["stable_hash"]), ref)
        by_position.setdefault((sn_lc, ref["row_index"]), ref)
    return {
        "section_label": by_section_label,
        "label": by_label,
        "hash": by_hash,
        "position": by_position,
    }


def _resolve_in_memory(
    index: _NodeIndex,
    ids: dict[str, str],
    *,
    allow_position_fallback: bool,
) -> tuple[dict, str] | None:
    """Try the four tiers in order against the pre-built node index. Returns
    ``(node_ref, tier)`` for the first match, or ``None``. Pure-Python — no
    DB round-trip per entry.

    Matching for sheet/section/label tiers is case-insensitive so common
    capitalization fixes in workbook text don't drop progress on the floor."""
    if v := ids.get("section_label"):
        m = _parse_id(v, "section_label")
        if m:
            ref = index["section_label"].get(
                (m["sheet"].lower(), m["section"].lower(), m["label"].lower())
            )
            if ref:
                return ref, "section_label"
    if v := ids.get("label"):
        m = _parse_id(v, "label")
        if m:
            ref = index["label"].get((m["sheet"].lower(), m["label"].lower()))
            if ref:
                return ref, "label"
    if v := ids.get("hash"):
        m = _parse_id(v, "hash")
        if m:
            ref = index["hash"].get((m["sheet"].lower(), m["hash"]))
            if ref:
                return ref, "hash"
    if allow_position_fallback and (v := ids.get("position")):
        m = _parse_id(v, "position")
        if m:
            ref = index["position"].get((m["sheet"].lower(), int(m["row"])))
            if ref:
                return ref, "position"
    return None


def _parse_id(value: str, kind: str) -> dict[str, str] | None:
    """Decode our pipe-delimited identity strings back into named parts."""
    parts = dict(p.split(":", 1) for p in value.split("|") if ":" in p)
    if kind == "section_label":
        if {"sheet", "section", "label"} <= parts.keys():
            return parts
    elif kind == "label":
        if {"sheet", "label"} <= parts.keys():
            return parts
    elif kind == "hash":
        if {"sheet", "hash"} <= parts.keys():
            return parts
    elif kind == "position":
        if {"sheet", "row"} <= parts.keys() and parts["row"].lstrip("-").isdigit():
            return parts
    return None


def _replay_sidecar(
    doc: dict,
    index: _NodeIndex,
    *,
    allow_position_fallback: bool,
    bootstrapped_count: int,
) -> tuple[CharacterReport, list[tuple], bool]:
    """Resolve every entry in ``doc`` against the in-memory node index.

    Returns ``(report, batch, dirty)`` where ``batch`` is the list of
    ``(sheet_name, row_index, state, value, ts)`` tuples ready for a single
    ``executemany`` into ``character_progress``. ``dirty`` says whether any
    entry's stored ids changed (refreshed to a stronger anchor) or its
    ``orphan`` flag flipped — caller decides whether to rewrite the file.
    """
    header = doc.get("character") or {}
    cr = CharacterReport(name=header.get("name") or "")
    cr.bootstrapped_from_db = bootstrapped_count
    batch: list[tuple] = []
    dirty = False
    now = _now_iso()

    for entry in doc.get("progress", []):
        if not isinstance(entry, dict):
            continue
        ids = entry.get("ids") or {}
        state = entry.get("state")
        if state not in ("done", "todo", "excluded"):
            continue
        resolved = _resolve_in_memory(
            index, ids, allow_position_fallback=allow_position_fallback,
        )
        if resolved is None:
            if not entry.get("orphan"):
                entry["orphan"] = True
                dirty = True
            cr.orphaned += 1
            continue
        ref, tier = resolved
        value = entry.get("value")
        batch.append((
            ref["sheet_name"], ref["row_index"], state,
            float(value) if value is not None else None,
            entry.get("ts") or now,
        ))
        setattr(cr, f"matched_{tier}", getattr(cr, f"matched_{tier}") + 1)

        # If the entry resolved via a weaker tier than its strongest available
        # anchor, rewrite ids so the next reconcile has a stronger handle.
        # The node ref already carries everything we need — no DB round-trip.
        fresh_ids = compute_stable_ids(
            ref["sheet_name"], ref["section_label"], ref["label"],
            ref["row_json"], ref["row_index"],
            precomputed_hash=ref["stable_hash"],
        )
        if fresh_ids != ids:
            entry["ids"] = fresh_ids
            dirty = True
        if entry.get("orphan"):
            entry.pop("orphan", None)
            dirty = True

    return cr, batch, dirty


def reconcile_all(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    allow_position_fallback: bool | None = None,
) -> ReconcileReport:
    """Make the DB match the sidecars for the current run.

    For each existing character with no sidecar, dump current DB progress to
    a fresh sidecar (one-time migration). For each sidecar, ensure the
    character exists, wipe the current run's DB rows for that character, and
    replay every entry that the tiered resolver can map to a live node.

    Entries that fail every enabled tier are flagged ``orphan`` in the sidecar
    and can be reviewed / resolved manually. By default, position fallback is
    disabled to avoid accidental mis-matches after row reordering; set
    ``allow_position_fallback=True`` (or env
    ``FFXIV_PROGRESS_ALLOW_POSITION_FALLBACK=1``) for aggressive recovery.

    Performance: the node set for the run is snapshotted once into in-memory
    indexes, and progress rows are inserted via ``executemany`` per character.
    A 100 %-completion sidecar (~37 k entries) replays in well under a second
    instead of multiple seconds of per-row round-trips.
    """
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    report = ReconcileReport()
    if allow_position_fallback is None:
        allow_position_fallback = ALLOW_POSITION_FALLBACK_DEFAULT

    # Step 1: bootstrap any characters with DB progress but no sidecar yet
    bootstrapped: dict[str, int] = {}
    chars = conn.execute(
        "SELECT id, name, starting_class, created_at FROM characters"
    ).fetchall()
    for char in chars:
        if not char["name"]:
            continue
        path = sidecar_path(char["name"])
        if path.exists():
            continue
        n = _bootstrap_from_db(conn, char, run_id)
        if n:
            bootstrapped[char["name"]] = n

    sidecars = list_sidecars()
    if not sidecars:
        conn.commit()
        return report

    # One bulk scan of nodes for the whole run -- shared across every sidecar.
    index = _build_node_index(conn, run_id)

    # Step 2: for every sidecar on disk, replay it into the DB
    for path in sidecars:
        doc = load_sidecar(path)
        if not doc:
            continue
        header = doc.get("character") or {}
        name = header.get("name")
        if not name:
            continue

        cid = _ensure_character(conn, name, header)
        if header.get("starting_class") is not None:
            conn.execute(
                "UPDATE characters SET starting_class = ? WHERE id = ?",
                (header["starting_class"], cid),
            )

        # Wipe this character's progress for the current run; we're about to
        # rebuild it from the sidecar (the sole source of truth).
        conn.execute(
            "DELETE FROM character_progress WHERE character_id = ? AND run_id = ?",
            (cid, run_id),
        )
        conn.execute(
            "DELETE FROM progress_rollup WHERE character_id = ?", (cid,)
        )

        cr, batch, dirty = _replay_sidecar(
            doc, index,
            allow_position_fallback=allow_position_fallback,
            bootstrapped_count=bootstrapped.get(name, 0),
        )
        # name from header may differ slightly from filename — trust the doc
        cr.name = name

        if batch:
            conn.executemany(
                """INSERT INTO character_progress
                   (character_id, run_id, sheet_name, row_index, state,
                    progress_percent, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(character_id, run_id, sheet_name, row_index)
                   DO UPDATE SET state = excluded.state,
                                 progress_percent = excluded.progress_percent,
                                 updated_at = excluded.updated_at""",
                [(cid, run_id, sn, ri, st, val, ts)
                 for (sn, ri, st, val, ts) in batch],
            )

        if dirty:
            save_sidecar(path, doc)
        report.characters.append(cr)

    conn.commit()
    # Reconcile rewrote disk-side state; drop any in-memory cache so the
    # next record_state_change re-reads from the now-authoritative files.
    invalidate_cache()
    return report
