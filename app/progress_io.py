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

_STRICT_WRITE_THROUGH_ENV = os.getenv(
    "FFXIV_PROGRESS_STRICT_WRITE_THROUGH", ""
).strip().lower()
STRICT_WRITE_THROUGH_DEFAULT = _STRICT_WRITE_THROUGH_ENV not in {
    "0", "false", "no", "off"
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
_SIDECAR_DIGEST_LEN = 12
_sidecar_resolution_lock = threading.Lock()
_resolved_sidecar_paths: dict[str, Path] = {}


def _normalize_character_name(character_name: str) -> str:
    return character_name.strip()


def _legacy_sidecar_filename(character_name: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", _normalize_character_name(character_name)) or "_unnamed"
    return f"{safe}.json"


def _canonical_sidecar_filename(character_name: str) -> str:
    normalized = _normalize_character_name(character_name)
    safe = _SAFE_NAME_RE.sub("_", normalized) or "_unnamed"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_SIDECAR_DIGEST_LEN]
    return f"{safe}__{digest}.json"


def _read_sidecar_owner_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    character = doc.get("character")
    if not isinstance(character, dict):
        return None
    name = character.get("name")
    if not isinstance(name, str):
        return None
    normalized = name.strip()
    return normalized or None


def _cache_sidecar_path(character_name: str, path: Path) -> None:
    with _sidecar_resolution_lock:
        _resolved_sidecar_paths[character_name] = path


def _cached_sidecar_path(character_name: str) -> Path | None:
    with _sidecar_resolution_lock:
        cached = _resolved_sidecar_paths.get(character_name)
    if cached is None:
        return None
    if cached.exists():
        return cached
    with _sidecar_resolution_lock:
        if _resolved_sidecar_paths.get(character_name) == cached:
            _resolved_sidecar_paths.pop(character_name, None)
    return None


def _promote_sidecar_to_canonical(path: Path, character_name: str) -> Path:
    """Rename legacy sidecars to canonical hashed filenames when safe."""
    normalized = _normalize_character_name(character_name)
    canonical = PROGRESS_DIR / _canonical_sidecar_filename(normalized)
    legacy = PROGRESS_DIR / _legacy_sidecar_filename(normalized)

    if path == canonical:
        _cache_sidecar_path(normalized, canonical)
        return canonical
    if path != legacy:
        return path
    if canonical.exists():
        quarantine = path.with_name(path.name + ".superseded")
        if quarantine.exists():
            suffix_idx = 1
            while True:
                candidate = path.with_name(path.name + f".superseded.{suffix_idx}")
                if not candidate.exists():
                    quarantine = candidate
                    break
                suffix_idx += 1
        try:
            path.replace(quarantine)
        except OSError:
            pass
        _cache_sidecar_path(normalized, canonical)
        return canonical

    try:
        path.replace(canonical)
    except OSError:
        return path

    _cache_sidecar_path(normalized, canonical)
    return canonical


def sidecar_path(character_name: str) -> Path:
    """Return the sidecar path for a character.

    Uses a canonical hashed filename to avoid collisions caused by filesystem-
    safe sanitization, but still honors legacy filenames when they already
    exist and appear to belong to this character.
    """
    normalized = _normalize_character_name(character_name)

    cached = _cached_sidecar_path(normalized)
    if cached is not None:
        return cached

    canonical = PROGRESS_DIR / _canonical_sidecar_filename(normalized)
    if canonical.exists():
        _cache_sidecar_path(normalized, canonical)
        return canonical

    legacy = PROGRESS_DIR / _legacy_sidecar_filename(normalized)
    if legacy.exists():
        owner = _read_sidecar_owner_name(legacy)
        if owner is None or owner == normalized:
            _cache_sidecar_path(normalized, legacy)
            return legacy

    _cache_sidecar_path(normalized, canonical)
    return canonical


def sidecar_path_for_delete(
    character_name: str,
    *,
    other_character_names: list[str] | None = None,
) -> Path | None:
    """Return a sidecar path safe to unlink for a deleted character.

    Legacy filenames can collide after sanitization. We only delete a legacy
    file when it appears owned by this character and no remaining character
    name maps to the same legacy filename.
    """
    normalized = _normalize_character_name(character_name)
    canonical = PROGRESS_DIR / _canonical_sidecar_filename(normalized)
    if canonical.exists():
        return canonical

    legacy = PROGRESS_DIR / _legacy_sidecar_filename(normalized)
    if not legacy.exists():
        return None

    owner = _read_sidecar_owner_name(legacy)
    if owner is not None and owner != normalized:
        return None

    for other in other_character_names or []:
        other_normalized = _normalize_character_name(other)
        if not other_normalized or other_normalized == normalized:
            continue
        if _legacy_sidecar_filename(other_normalized) == legacy.name:
            return None

    return legacy


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
_io_gate = threading.RLock()
_doc_cache: dict[Path, dict] = {}
_path_locks: dict[Path, threading.RLock] = {}
_batch_depth: dict[Path, int] = {}
_batch_failed: dict[Path, bool] = {}
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
    with _io_gate:
        with lock:
            _batch_depth[path] = _batch_depth.get(path, 0) + 1
            _batch_failed.setdefault(path, False)
        try:
            yield
        except Exception:
            with lock:
                _batch_failed[path] = True
            raise
        finally:
            with lock:
                depth = _batch_depth.get(path, 1) - 1
                if depth <= 0:
                    _batch_depth.pop(path, None)
                    failed = _batch_failed.pop(path, False)
                    if failed:
                        _dirty.discard(path)
                        with _cache_lock:
                            _doc_cache.pop(path, None)
                    else:
                        _flush(path)
                else:
                    _batch_depth[path] = depth


def _raise_or_warn_write_through(msg: str) -> None:
    if STRICT_WRITE_THROUGH_DEFAULT:
        raise ValueError(msg)
    print(f"Progress sidecar warning: {msg}")


def _in_active_batch(path: Path | None = None) -> bool:
    if path is not None:
        return _batch_depth.get(path, 0) > 0
    return any(depth > 0 for depth in _batch_depth.values())


def _handle_write_through_gap(
    msg: str,
    *,
    path: Path | None = None,
    soft_fail_in_batch: bool = False,
) -> None:
    if (
        STRICT_WRITE_THROUGH_DEFAULT
        and not (soft_fail_in_batch and _in_active_batch(path))
    ):
        raise ValueError(msg)
    print(f"Progress sidecar warning: {msg}")


@contextmanager
def sidecar_io_gate():
    """Serialize sidecar reconcile against live writes."""
    with _io_gate:
        yield


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


def _select_update_match(progress: list, target_ids: dict[str, str]) -> int | None:
    """Pick an existing entry to update, or None when ambiguous.

    Matching must be deterministic and row-compatible to avoid collapsing two
    rows that share sheet/section/label keys.
    """
    hash_id = target_ids.get("hash")
    section_id = target_ids.get("section_label")
    label_id = target_ids.get("label")
    pos_id = target_ids.get("position")

    candidates: list[tuple[int, dict[str, str]]] = []
    for i, entry in enumerate(progress):
        if not isinstance(entry, dict):
            continue
        entry_ids = entry.get("ids")
        if not isinstance(entry_ids, dict):
            continue
        candidates.append((i, entry_ids))

    if not candidates:
        return None

    if hash_id:
        by_hash = [
            (i, ids)
            for (i, ids) in candidates
            if ids.get("hash") == hash_id
        ]
        if pos_id and by_hash:
            by_hash_pos = [i for (i, ids) in by_hash if ids.get("position") == pos_id]
            if len(by_hash_pos) == 1:
                return by_hash_pos[0]
        if len(by_hash) == 1:
            only_idx, only_ids = by_hash[0]
            only_pos = only_ids.get("position")
            if not only_pos or (pos_id and only_pos == pos_id):
                return only_idx

    if section_id and pos_id:
        by_section_pos = [
            i
            for (i, ids) in candidates
            if ids.get("section_label") == section_id and ids.get("position") == pos_id
        ]
        if len(by_section_pos) == 1:
            return by_section_pos[0]

    if label_id and pos_id:
        by_label_pos = [
            i
            for (i, ids) in candidates
            if ids.get("label") == label_id and ids.get("position") == pos_id
        ]
        if len(by_label_pos) == 1:
            return by_label_pos[0]

    if pos_id:
        by_pos = [i for (i, ids) in candidates if ids.get("position") == pos_id]
        if len(by_pos) == 1:
            return by_pos[0]

    return None


def _select_remove_indexes(progress: list, target_ids: dict[str, str]) -> list[int]:
    """Return entry indexes that safely map to one row override to remove.

    Remove matching mirrors update matching to avoid deleting entries that
    wouldn't qualify as the same row during updates. When position is unique,
    we still collapse duplicate entries at that same position.
    """
    hash_id = target_ids.get("hash")
    section_id = target_ids.get("section_label")
    label_id = target_ids.get("label")
    pos_id = target_ids.get("position")

    match_idx = _select_update_match(progress, target_ids)
    if match_idx is None and not pos_id:
        return []

    if not pos_id and match_idx is not None:
        return [match_idx]

    candidates: list[tuple[int, dict[str, str]]] = []
    for i, entry in enumerate(progress):
        if not isinstance(entry, dict):
            continue
        entry_ids = entry.get("ids")
        if not isinstance(entry_ids, dict):
            continue
        candidates.append((i, entry_ids))

    remove_indexes: list[int] = []
    for i, ids in candidates:
        if ids.get("position") != pos_id:
            continue
        if hash_id and ids.get("hash") and ids.get("hash") != hash_id:
            continue
        if section_id and ids.get("section_label") and ids.get("section_label") != section_id:
            continue
        if label_id and ids.get("label") and ids.get("label") != label_id:
            continue
        remove_indexes.append(i)

    if match_idx is not None and match_idx not in remove_indexes:
        remove_indexes.append(match_idx)
    return sorted(set(remove_indexes))


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
    section / hash. If a lookup fails, strict mode raises so the DB write can
    roll back and we don't silently diverge DB from sidecar state."""
    with _io_gate:
        char = conn.execute(
            "SELECT name, starting_class, created_at FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
        if not char or not char["name"]:
            _raise_or_warn_write_through(
                f"missing character metadata for id={character_id}"
            )
            return
        path = sidecar_path(char["name"])
        node = _node_for_row(conn, run_id, sheet_name, row_index)
        if not node:
            _handle_write_through_gap(
                "missing node for write-through "
                f"(character_id={character_id}, run_id={run_id}, "
                f"sheet={sheet_name}, row={row_index})",
                path=path,
                soft_fail_in_batch=True,
            )
            return

        ids = compute_stable_ids(
            sheet_name, node["section_label"], node["label"],
            node["row_json"], row_index,
            precomputed_hash=node["stable_hash"] or None,
        )

        new_entry: dict = {
            "ids": ids,
            "state": state,
            "ts": _now_iso(),
        }
        if progress_percent is not None:
            new_entry["value"] = progress_percent

        # Serialize concurrent writers on the same sidecar. Under a chain
        # cascade the same thread reenters this for ~N rows; the outer batch()
        # holds the RLock for the whole cascade so contention is rare.
        char_dict = dict(char)
        with _path_lock(path):
            doc = _get_doc(path, lambda: _new_doc(char_dict))

            match_idx = _select_update_match(doc["progress"], ids)

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


def remove_state_change(
    conn: sqlite3.Connection,
    character_id: int,
    run_id: int,
    sheet_name: str,
    row_index: int,
) -> None:
    """Remove a row's explicit progress entry from the character sidecar.

    Called when DB override rows are deleted (restore inherited baseline state).
    """
    with _io_gate:
        char = conn.execute(
            "SELECT name, starting_class, created_at FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
        if not char or not char["name"]:
            _raise_or_warn_write_through(
                f"missing character metadata for id={character_id}"
            )
            return
        path = sidecar_path(char["name"])
        node = _node_for_row(conn, run_id, sheet_name, row_index)
        if not node:
            _handle_write_through_gap(
                "missing node for remove write-through "
                f"(character_id={character_id}, run_id={run_id}, "
                f"sheet={sheet_name}, row={row_index})",
                path=path,
                soft_fail_in_batch=True,
            )
            return

        ids = compute_stable_ids(
            sheet_name,
            node["section_label"],
            node["label"],
            node["row_json"],
            row_index,
            precomputed_hash=node["stable_hash"] or None,
        )

        char_dict = dict(char)
        with _path_lock(path):
            doc = _get_doc(path, lambda: _new_doc(char_dict))
            progress = doc.get("progress")
            if not isinstance(progress, list) or not progress:
                return

            remove_indexes = set(_select_remove_indexes(progress, ids))
            if not remove_indexes:
                return

            doc["progress"] = [
                entry for idx, entry in enumerate(progress) if idx not in remove_indexes
            ]
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
    preexisting_db_rows: int = 0
    replayed_rows: int = 0


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
                + (f"   db_rows {c.preexisting_db_rows}->{c.replayed_rows}"
                    if c.preexisting_db_rows or c.replayed_rows else "")
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
    if cur.lastrowid is None:
        raise ValueError(f"Could not create character '{name}'")
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


_NodeIndex = dict[str, dict[tuple, list[dict[str, object]]]]


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
    by_section_label: dict[tuple, list[dict[str, object]]] = {}
    by_label: dict[tuple, list[dict[str, object]]] = {}
    by_hash: dict[tuple, list[dict[str, object]]] = {}
    by_position: dict[tuple, list[dict[str, object]]] = {}
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
                (sn_lc, ref["section_label"].lower(), ref["label"].lower()), []
            ).append(ref)
        if ref["label"]:
            by_label.setdefault((sn_lc, ref["label"].lower()), []).append(ref)
        if ref["stable_hash"]:
            by_hash.setdefault((sn_lc, ref["stable_hash"]), []).append(ref)
        by_position.setdefault((sn_lc, ref["row_index"]), []).append(ref)
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
    parsed = {
        "section_label": _parse_id(ids.get("section_label", ""), "section_label")
        if ids.get("section_label") else None,
        "label": _parse_id(ids.get("label", ""), "label")
        if ids.get("label") else None,
        "hash": _parse_id(ids.get("hash", ""), "hash")
        if ids.get("hash") else None,
        "position": _parse_id(ids.get("position", ""), "position")
        if ids.get("position") else None,
    }

    def _row_index_of(ref: dict[str, object], *, default: int = -1) -> int:
        value = ref.get("row_index")
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def _disambiguate(refs: list[dict[str, object]]) -> list[dict[str, object]]:
        out = list(refs)
        hash_parts = parsed.get("hash")
        if hash_parts and len(out) > 1:
            narrowed = [
                r for r in out
                if str(r.get("stable_hash") or "") == str(hash_parts["hash"])
            ]
            if narrowed:
                out = narrowed

        section_parts = parsed.get("section_label")
        if section_parts and len(out) > 1:
            narrowed = [
                r for r in out
                if str(r.get("sheet_name") or "").lower() == str(section_parts["sheet"]).lower()
                and str(r.get("section_label") or "").lower() == str(section_parts["section"]).lower()
                and str(r.get("label") or "").lower() == str(section_parts["label"]).lower()
            ]
            if narrowed:
                out = narrowed

        label_parts = parsed.get("label")
        if label_parts and len(out) > 1:
            narrowed = [
                r for r in out
                if str(r.get("sheet_name") or "").lower() == str(label_parts["sheet"]).lower()
                and str(r.get("label") or "").lower() == str(label_parts["label"]).lower()
            ]
            if narrowed:
                out = narrowed

        pos_parts = parsed.get("position")
        if pos_parts and len(out) > 1:
            row_value = int(pos_parts["row"])
            narrowed = [r for r in out if _row_index_of(r) == row_value]
            if narrowed:
                out = narrowed

        # If strict identity keys still leave multiple candidates, pick a
        # deterministic nearest row to avoid unnecessary orphaning after small
        # workbook shifts (insertions/removals around a section).
        if pos_parts and len(out) > 1:
            row_value = int(pos_parts["row"])
            ranked = sorted(
                out,
                key=lambda r: (
                    abs(_row_index_of(r, default=row_value) - row_value),
                    _row_index_of(r, default=row_value),
                ),
            )
            if ranked:
                best_distance = abs(
                    _row_index_of(ranked[0], default=row_value) - row_value
                )
                best = [
                    r
                    for r in ranked
                    if abs(_row_index_of(r, default=row_value) - row_value) == best_distance
                ]
                if len(best) == 1:
                    out = [best[0]]

        return out

    section_parts = parsed.get("section_label")
    if section_parts:
        refs = index["section_label"].get(
            (
                str(section_parts["sheet"]).lower(),
                str(section_parts["section"]).lower(),
                str(section_parts["label"]).lower(),
            ),
            [],
        )
        refs = _disambiguate(refs)
        if len(refs) == 1:
            return refs[0], "section_label"

    label_parts = parsed.get("label")
    if label_parts:
        refs = index["label"].get(
            (str(label_parts["sheet"]).lower(), str(label_parts["label"]).lower()),
            [],
        )
        refs = _disambiguate(refs)
        if len(refs) == 1:
            return refs[0], "label"

    hash_parts = parsed.get("hash")
    if hash_parts:
        refs = index["hash"].get(
            (str(hash_parts["sheet"]).lower(), str(hash_parts["hash"])),
            [],
        )
        refs = _disambiguate(refs)
        if len(refs) == 1:
            return refs[0], "hash"

    pos_parts = parsed.get("position")
    if allow_position_fallback and pos_parts:
        refs = index["position"].get(
            (str(pos_parts["sheet"]).lower(), int(pos_parts["row"])),
            [],
        )
        refs = _disambiguate(refs)
        if len(refs) == 1:
            return refs[0], "position"

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
    with _io_gate:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        report = ReconcileReport()
        if allow_position_fallback is None:
            allow_position_fallback = ALLOW_POSITION_FALLBACK_DEFAULT

        # Ensure reconcile reads authoritative disk state, not stale in-memory
        # docs that might still be dirty from earlier writes.
        invalidate_cache()

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

        # One bulk scan of nodes for the whole run -- shared across sidecars.
        index = _build_node_index(conn, run_id)
        processed_paths: set[Path] = set()

        # Step 2: for every sidecar on disk, replay it into the DB
        for path in sidecars:
            doc = load_sidecar(path)
            if not doc:
                continue
            header = doc.get("character") or {}
            name = header.get("name")
            if not name:
                continue
            name = str(name).strip()
            if not name:
                continue
            source_path = path
            path = _promote_sidecar_to_canonical(source_path, name)
            if path != source_path:
                promoted_doc = load_sidecar(path)
                if promoted_doc:
                    doc = promoted_doc
            if path in processed_paths:
                continue
            processed_paths.add(path)

            cid = _ensure_character(conn, name, header)
            if header.get("starting_class") is not None:
                conn.execute(
                    "UPDATE characters SET starting_class = ? WHERE id = ?",
                    (header["starting_class"], cid),
                )

            existing_count_row = conn.execute(
                "SELECT COUNT(*) AS c FROM character_progress "
                "WHERE character_id = ? AND run_id = ?",
                (cid, run_id),
            ).fetchone()

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
                doc,
                index,
                allow_position_fallback=allow_position_fallback,
                bootstrapped_count=bootstrapped.get(name, 0),
            )
            # name from header may differ slightly from filename — trust the doc
            cr.name = name
            cr.preexisting_db_rows = int(existing_count_row["c"] if existing_count_row else 0)
            cr.replayed_rows = len(batch)

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
