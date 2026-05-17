"""FFXIV Completion Tracker — interactive launcher menu.

Invoked by launch.cmd after the virtualenv and dependencies are ready.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from threading import Timer

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
REQUIREMENTS = ROOT / "requirements.txt"
CONFIG_PATH = ROOT / ".launch.json"

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "ffxiv_tracker.sqlite"
PROGRESS_DIR = DATA_DIR / "progress"
PROBE_DIR = DATA_DIR / "lodestone_probe"
SPREADSHEET_DIR = ROOT / "Spreadsheet"
BACKUP_DIR = ROOT / "backups"

DEFAULT_HOST = "127.0.0.1"
PORT = 8000

# Windows process creation flag — spawn in a new console window.
CREATE_NEW_CONSOLE = 0x00000010

DISCORD_INVITE_URL = "https://discord.gg/S456xWWVyd"


# ---------------------------------------------------------------------------
# Config + small utilities
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def current_host() -> str:
    return load_config().get("host", DEFAULT_HOST)


def detect_lan_ip() -> str | None:
    # UDP connect() just sets a destination; no packet leaves the machine.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def browser_host(bind_host: str) -> str:
    if bind_host in {"0.0.0.0", "127.0.0.1"}:
        return "127.0.0.1"
    return bind_host


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def port_in_use(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def banner() -> None:
    py_ver = sys.version.split()[0]
    host = current_host()
    if host == "127.0.0.1":
        host_label = f"{host}  (loopback)"
    elif host == "0.0.0.0":
        host_label = f"{host}  (all interfaces - LAN reachable)"
    else:
        host_label = f"{host}  (LAN bind)"
    bar = "=" * 60
    print()
    print(bar)
    print(" FFXIV Completion Tracker")
    print(bar)
    print(f" Python {py_ver}   venv: {VENV_PY.parent.parent.relative_to(ROOT)}\\")
    print(f" Bind host: {host_label}")
    print()


def run_ingest() -> None:
    print("\n[ingest] Building SQLite from newest Spreadsheet/*.xlsx ...\n")
    subprocess.call([str(VENV_PY), str(ROOT / "scripts" / "prep_xlsx_to_sqlite.py")])
    input("\n[ingest] Done. Press Enter to return to the menu.")


def reinstall_dependencies() -> None:
    print("\n[deps] Reinstalling / upgrading from requirements.txt ...\n")
    subprocess.call([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.call([
        str(VENV_PY), "-m", "pip", "install",
        "--upgrade", "-r", str(REQUIREMENTS),
    ])
    input("\n[deps] Done. Press Enter to return to the menu.")


def show_status() -> None:
    print()
    print("  --- Workbook & DB ---")
    if SPREADSHEET_DIR.exists():
        xlsx = sorted(SPREADSHEET_DIR.glob("*.xlsx"))
        print(f"  Spreadsheet/   {len(xlsx)} .xlsx file(s)")
        if xlsx:
            newest = max(xlsx, key=lambda p: p.stat().st_mtime)
            mtime = dt.datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"                 newest: {newest.name}  ({mtime})")
    else:
        print("  Spreadsheet/   (missing)")

    if DB_PATH.exists():
        print(f"  Database       {DB_PATH.name}  {fmt_bytes(DB_PATH.stat().st_size)}")
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                run = conn.execute(
                    "SELECT id, source_file, started_at, completed_at, sheet_count, row_count "
                    "FROM ingest_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if run is None:
                    print("  Latest ingest  (no runs recorded)")
                else:
                    print(f"  Latest ingest  run #{run['id']}  source: {run['source_file']}")
                    print(f"                 started:   {run['started_at']}")
                    print(f"                 completed: {run['completed_at'] or '(in progress?)'}")
                    print(f"                 sheets={run['sheet_count']}  rows={run['row_count']}")
                chars = conn.execute(
                    "SELECT name, starting_class FROM characters ORDER BY name"
                ).fetchall()
                print(f"  Characters     {len(chars)}")
                for c in chars:
                    cls = f" [{c['starting_class']}]" if c["starting_class"] else ""
                    print(f"                  - {c['name']}{cls}")
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"  DB query error: {e}")
    else:
        print("  Database       (no DB; run ingest first)")

    print()
    print("  --- Storage ---")
    sidecar_count = len(list(PROGRESS_DIR.glob("*.json"))) if PROGRESS_DIR.exists() else 0
    print(f"  data/                  {fmt_bytes(dir_size(DATA_DIR))}")
    print(f"    progress/            {fmt_bytes(dir_size(PROGRESS_DIR))}  ({sidecar_count} sidecars)")
    print(f"    lodestone_probe/     {fmt_bytes(dir_size(PROBE_DIR))}")
    if BACKUP_DIR.exists():
        backups = sorted(BACKUP_DIR.glob("data_*.zip"))
        print(f"  backups/               {fmt_bytes(dir_size(BACKUP_DIR))}  ({len(backups)} archive(s))")

    print()
    print("  --- Server ---")
    host = current_host()
    if port_in_use("127.0.0.1", PORT):
        print(f"  Listening on 127.0.0.1:{PORT}   ->   http://127.0.0.1:{PORT}")
    else:
        print(f"  Not running  (configured to bind {host}:{PORT})")

    print()
    input("  Press Enter to return to the menu.")


def open_data_folder() -> None:
    folders = [
        ("Project root",          ROOT),
        ("Spreadsheet/",          SPREADSHEET_DIR),
        ("data/",                 DATA_DIR),
        ("data/progress/",        PROGRESS_DIR),
        ("data/lodestone_probe/", PROBE_DIR),
        ("backups/",              BACKUP_DIR),
    ]
    print()
    for idx, (label, path) in enumerate(folders, start=1):
        suffix = "" if path.exists() else "   (does not exist yet)"
        print(f"  {idx}) {label}{suffix}")
    print("  b) Back")
    print()
    choice = input("  > ").strip().lower()
    if choice in {"b", "", "back"}:
        return
    if not choice.isdigit():
        print("  Invalid choice.")
        time.sleep(0.6)
        return
    n = int(choice)
    if not (1 <= n <= len(folders)):
        return
    _, path = folders[n - 1]
    if not path.exists():
        print(f"  {path} does not exist yet.")
        time.sleep(0.8)
        return
    try:
        os.startfile(str(path))
    except OSError as e:
        print(f"  Could not open: {e}")
        time.sleep(1.0)


def backup_data() -> None:
    if not DB_PATH.exists() and not PROGRESS_DIR.exists():
        print("\n  Nothing to back up — no DB and no progress sidecars yet.")
        time.sleep(1.2)
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = BACKUP_DIR / f"data_{ts}"

    # Stage the parts we want, then zip the stage. lodestone_probe/ is excluded
    # because it's regenerable and tends to be the bulky part of data/.
    work = BACKUP_DIR / f".staging_{ts}"
    try:
        work.mkdir(parents=True, exist_ok=False)
        staged_data = work / "data"
        staged_data.mkdir()

        if DB_PATH.exists():
            shutil.copy2(DB_PATH, staged_data / DB_PATH.name)
        if PROGRESS_DIR.exists():
            shutil.copytree(PROGRESS_DIR, staged_data / "progress")

        archive = shutil.make_archive(str(base), "zip", str(work))
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

    size = Path(archive).stat().st_size
    print(f"\n  Backup written: {Path(archive).name}  ({fmt_bytes(size)})")
    print(f"  Location: {BACKUP_DIR}")
    print("  Includes: data/ffxiv_tracker.sqlite + data/progress/")
    print("  Excludes: data/lodestone_probe/ (regenerable)")
    input("\n  Press Enter to return to the menu.")


def clean_lodestone_artifacts() -> None:
    if not PROBE_DIR.exists():
        print(f"\n  {PROBE_DIR} does not exist yet — nothing to clean.")
        time.sleep(1.2)
        return

    # NOTE: data/progress/ (character sidecars) is intentionally NOT in this list.
    sections: list[tuple[str, list[Path]]] = [
        ("logs/",                [p for p in (PROBE_DIR / "logs").rglob("*")
                                  if p.is_file()] if (PROBE_DIR / "logs").exists() else []),
        ("import_logs/",         [p for p in (PROBE_DIR / "import_logs").rglob("*")
                                  if p.is_file()] if (PROBE_DIR / "import_logs").exists() else []),
        ("import_uploads/",      [p for p in (PROBE_DIR / "import_uploads").rglob("*")
                                  if p.is_file()] if (PROBE_DIR / "import_uploads").exists() else []),
        ("unmatched/",           [p for p in (PROBE_DIR / "unmatched").rglob("*")
                                  if p.is_file()] if (PROBE_DIR / "unmatched").exists() else []),
        ("*.json payloads",      list(PROBE_DIR.glob("*.json"))),
    ]

    print(f"\n  Cleaning under {PROBE_DIR}\\")
    print("  (data/progress/ character sidecars are NOT touched)\n")

    total_files = 0
    total_bytes = 0
    for label, files in sections:
        size = sum((p.stat().st_size for p in files if p.exists()), 0)
        total_files += len(files)
        total_bytes += size
        print(f"  {label:24s} {len(files):4d} files   {fmt_bytes(size)}")
    print(f"  {'TOTAL':24s} {total_files:4d} files   {fmt_bytes(total_bytes)}")

    if total_files == 0:
        print("\n  Nothing to delete.")
        input("  Press Enter to return to the menu.")
        return

    print()
    raw = input("  Delete files older than how many days?  (default 14, 0 = delete all, b = back): ").strip().lower()
    if raw in {"b", "back"}:
        return
    if raw == "":
        days = 14
    else:
        try:
            days = int(raw)
            if days < 0:
                print("  Negative not allowed.")
                time.sleep(0.8)
                return
        except ValueError:
            print("  Not a number.")
            time.sleep(0.8)
            return

    if days == 0:
        confirm = input("  Delete ALL listed files? Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("  Aborted.")
            time.sleep(0.8)
            return

    cutoff = time.time() - days * 86400 if days > 0 else None

    deleted = 0
    freed = 0
    for _, files in sections:
        for f in files:
            try:
                st = f.stat()
                if cutoff is None or st.st_mtime < cutoff:
                    size = st.st_size
                    f.unlink()
                    deleted += 1
                    freed += size
            except OSError:
                pass

    print(f"\n  Deleted {deleted} file(s)  ({fmt_bytes(freed)})")
    input("  Press Enter to return to the menu.")


def set_bind_ip() -> None:
    host = current_host()
    detected = detect_lan_ip()
    print(f"\n  Current bind host: {host}")
    if detected:
        print(f"  Detected LAN IP:   {detected}")
    else:
        print("  Detected LAN IP:   (none found)")
    print()
    print("  1) Loopback only (127.0.0.1) - safest, this machine only")
    print("  2) All interfaces (0.0.0.0)  - reachable from LAN")
    if detected:
        print(f"  3) Specific LAN IP ({detected}) - bind only to this interface")
    print("  c) Custom IP address")
    print("  b) Back to main menu")
    print()
    choice = input("  > ").strip().lower()

    new_host: str | None = None
    if choice == "1":
        new_host = "127.0.0.1"
    elif choice == "2":
        new_host = "0.0.0.0"
    elif choice == "3" and detected:
        new_host = detected
    elif choice == "c":
        raw = input("  Enter IP to bind: ").strip()
        if raw:
            try:
                socket.inet_aton(raw)
                new_host = raw
            except OSError:
                print("  Not a valid IPv4 address.")
                time.sleep(1.0)
                return
    elif choice in {"b", ""}:
        return
    else:
        print("  Invalid choice.")
        time.sleep(0.6)
        return

    if new_host is None:
        return

    cfg = load_config()
    cfg["host"] = new_host
    save_config(cfg)
    print(f"\n  Saved bind host: {new_host}")
    if new_host != "127.0.0.1":
        print("  Reminder: the app has no auth - only bind beyond loopback on networks you trust.")
    time.sleep(1.2)


def open_discord_invite() -> None:
    print(f"\n  Opening {DISCORD_INVITE_URL}")
    print("  Your browser may offer to open it in the Discord app if installed.")
    try:
        webbrowser.open(DISCORD_INVITE_URL)
    except Exception as e:
        print(f"  Could not open: {e}")
    time.sleep(1.0)


def start_server_and_open_browser() -> None:
    host = current_host()
    open_url = f"http://{browser_host(host)}:{PORT}"

    if port_in_use("127.0.0.1", PORT):
        print(f"\n[server] Something is already listening on 127.0.0.1:{PORT}.")
        print(f"[server] Opening {open_url} in the browser.")
        webbrowser.open(open_url)
        time.sleep(1.0)
        return

    # Spawn uvicorn in its OWN console window so this menu stays usable.
    # cmd /k keeps the spawned window open after uvicorn exits, so you can
    # read any final traceback. Close that window (or Ctrl+C inside it) to
    # stop the server.
    spawn_cmd = [
        "cmd", "/k",
        str(VENV_PY), "-m", "uvicorn", "app.main:app",
        "--host", host, "--port", str(PORT), "--reload",
    ]
    try:
        subprocess.Popen(
            spawn_cmd,
            creationflags=CREATE_NEW_CONSOLE,
            cwd=str(ROOT),
        )
    except OSError as e:
        print(f"\n[server] Failed to spawn server window: {e}")
        time.sleep(1.5)
        return

    print()
    print("[server] Launched uvicorn in a new console window.")
    print(f"[server] Bind:    http://{host}:{PORT}")
    print(f"[server] Browser: {open_url}")
    print("[server] Close that window (or Ctrl+C inside it) to stop the server.")

    # Give uvicorn a moment to bind, then open the browser.
    Timer(1.8, lambda: webbrowser.open(open_url)).start()
    time.sleep(1.4)


MENU: list[tuple[str, callable]] = [
    ("Status / health check",                show_status),
    ("Ingest workbook (.xlsx -> SQLite)",    run_ingest),
    ("Open data folder in Explorer",         open_data_folder),
    ("Backup data/ to a dated zip",          backup_data),
    ("Clean Lodestone probe artifacts",      clean_lodestone_artifacts),
    ("Reinstall / upgrade dependencies",     reinstall_dependencies),
    ("Set bind IP for LAN access",           set_bind_ip),
    ("Open FFXIV Completionist Discord",     open_discord_invite),
    ("Start server + open browser",          start_server_and_open_browser),
]


def prompt_choice() -> int | None:
    banner()
    for idx, (label, _) in enumerate(MENU, start=1):
        print(f"  {idx}) {label}")
    print("  q) Quit")
    print()
    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if choice in {"q", "quit", "exit"}:
        return None
    if not choice.isdigit():
        return -1
    n = int(choice)
    if 1 <= n <= len(MENU):
        return n - 1
    return -1


def main() -> int:
    while True:
        idx = prompt_choice()
        if idx is None:
            print("  Bye.")
            return 0
        if idx < 0:
            print("  Invalid choice.")
            time.sleep(0.6)
            continue
        MENU[idx][1]()


if __name__ == "__main__":
    raise SystemExit(main())
