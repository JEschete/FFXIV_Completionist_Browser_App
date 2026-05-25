"""
Build an Inno Setup installer for FFXIVTracker.

Usage:
    python build_installer.py [--version v1.0.0] [--python 3.13.3] [--skip-python]

Steps:
  1. Download Python embeddable zip (cached in build/cache/ after first run)
  2. Prepare the embed: enable site-packages, bootstrap pip, install deps
  3. Stage app source files into build/stage/ alongside the python/ runtime
  4. Compile the installer with ISCC

Prerequisites:
  - Inno Setup 6  (https://jrsoftware.org/isdl.php)
  - Internet access on first run (to download the Python embed + pip)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
BUILD_DIR = ROOT / "build"
STAGE_DIR = BUILD_DIR / "stage"
PYTHON_DIR = BUILD_DIR / "python"
CACHE_DIR = BUILD_DIR / "cache"

PYTHON_DEFAULT_VERSION = "3.13.3"
_PY_URL = "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip"
_GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

_ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]

# Mirror the inclusions from build_release.py
INCLUDE_FILES = [
    "launch.cmd",
    "launch_gui.cmd",
    "launch.py",
    "launch_gui.py",
    "updater.py",
    "_version.py",
    "requirements.txt",
    "README.md",
    "changelog.md",
    "LICENSE",
]

INCLUDE_DIRS: dict[str, dict] = {
    "app": {
        "exclude": ["__pycache__", "*.pyc", "avatars"],
    },
    "CharacterScraping": {
        "include_only": {"lodestone_probe.py"},
    },
    "GameDataReferences": {
        "include_only": {"quests.jsonl", "categories.json", "chains.json"},
    },
    "Spreadsheet": {
        "include_only": {"place_official_worksheet_here"},
    },
    "scripts": {
        "include_only": {"prep_xlsx_to_sqlite.py", "report_desktop_collisions.py"},
    },
    "assets": {
        "include_only": {"icon.png", "icon.ico"},
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_iscc() -> Path:
    found = shutil.which("ISCC") or shutil.which("ISCC.exe")
    if found:
        return Path(found)
    for p in _ISCC_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError(
        "Inno Setup (ISCC.exe) not found.\n"
        "Download and install from https://jrsoftware.org/isdl.php"
    )


def get_version() -> str:
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=ROOT, stderr=subprocess.DEVNULL, text=True,
        )
        return out.strip()
    except subprocess.CalledProcessError:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, text=True,
        ).strip()
        return f"dev-{sha}"


def _download(url: str, dest: Path, label: str) -> None:
    if dest.exists():
        print(f"  cached   {label}")
        return
    print(f"  fetching {label} ...", end="", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    print(f"  {dest.stat().st_size // 1024} KB")


def _is_excluded(rel: Path, exclude: list[str]) -> bool:
    for part in rel.parts:
        if any(Path(part).match(p) for p in exclude):
            return True
    return False


# ---------------------------------------------------------------------------
# Step 1 – Python embed
# ---------------------------------------------------------------------------

def setup_python_embed(py_version: str) -> None:
    zip_name = f"python-{py_version}-embed-amd64.zip"
    zip_path = CACHE_DIR / zip_name
    get_pip = CACHE_DIR / "get-pip.py"

    _download(_PY_URL.format(ver=py_version), zip_path, zip_name)
    _download(_GET_PIP_URL, get_pip, "get-pip.py")

    if PYTHON_DIR.exists():
        shutil.rmtree(PYTHON_DIR)
    PYTHON_DIR.mkdir(parents=True)

    print("  extracting embed ...", end="", flush=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(PYTHON_DIR)
    print("  done")

    # Enable site-packages in the embed's path configuration file.
    pth_files = list(PYTHON_DIR.glob("python*._pth"))
    if not pth_files:
        raise FileNotFoundError("No python*._pth found — unexpected embed structure")
    pth_file = pth_files[0]
    pth_text = pth_file.read_text(encoding="utf-8")
    pth_text = pth_text.replace("#import site", "import site")
    if "Lib\\site-packages" not in pth_text and "Lib/site-packages" not in pth_text:
        pth_text += "\nLib\\site-packages\n"
    pth_file.write_text(pth_text, encoding="utf-8")

    (PYTHON_DIR / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)

    py_exe = PYTHON_DIR / "python.exe"

    print("  bootstrapping pip ...")
    subprocess.check_call([str(py_exe), str(get_pip), "--no-warn-script-location"],
                          cwd=str(PYTHON_DIR))

    print("  installing dependencies ...")
    subprocess.check_call([
        str(py_exe), "-m", "pip", "install",
        "-r", str(ROOT / "requirements.txt"),
        "--no-warn-script-location",
    ])

    print(f"  embed ready  ({PYTHON_DIR.relative_to(ROOT)})")


# ---------------------------------------------------------------------------
# Step 2 – Stage app files
# ---------------------------------------------------------------------------

def regenerate_icon() -> None:
    """Convert assets/icon.png → assets/icon.ico so shortcuts have a proper
    Windows icon. PyQt6 is already a project dependency, so this needs no
    extra build deps."""
    src = ROOT / "assets" / "icon.png"
    dst = ROOT / "assets" / "icon.ico"
    if not src.exists():
        print(f"  WARN  {src} not found — skipping icon regeneration")
        return
    # PyQt6 needs a QGuiApplication before any QImage I/O. Importing inside
    # the function keeps build_installer.py importable even without PyQt6
    # (e.g. for a CI lint pass).
    from PyQt6.QtGui import QGuiApplication

    from build_icons import png_to_ico
    _ = QGuiApplication.instance() or QGuiApplication(sys.argv)
    png_to_ico(src, dst)
    print(f"  +  assets/icon.ico  ({dst.stat().st_size // 1024} KB, regenerated from icon.png)")


def stage_app_files() -> None:
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True)

    for name in INCLUDE_FILES:
        src = ROOT / name
        if not src.exists():
            print(f"  WARN  {name} not found, skipping")
            continue
        shutil.copy2(src, STAGE_DIR / name)
        print(f"  +  {name}")

    for dir_name, opts in INCLUDE_DIRS.items():
        src = ROOT / dir_name
        if not src.exists():
            print(f"  WARN  {dir_name}/ not found, skipping")
            continue
        dst = STAGE_DIR / dir_name
        dst.mkdir(parents=True, exist_ok=True)
        include_only: set[str] | None = opts.get("include_only")
        exclude: list[str] = opts.get("exclude", [])
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(src)
            if include_only is not None and rel.parts[0] not in include_only:
                continue
            if exclude and _is_excluded(rel, exclude):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        print(f"  +  {dir_name}/")

    if not PYTHON_DIR.exists():
        print("  WARN  build/python/ missing — run without --skip-python first")
        return
    print("  +  python/  (copying embed ...)", end="", flush=True)
    shutil.copytree(PYTHON_DIR, STAGE_DIR / "python")
    size_mb = sum(f.stat().st_size for f in (STAGE_DIR / "python").rglob("*") if f.is_file()) // 1024 // 1024
    print(f"  {size_mb} MB")


# ---------------------------------------------------------------------------
# Step 3 – Compile installer
# ---------------------------------------------------------------------------

def compile_installer(version: str, iscc: Path) -> Path:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DMyStageDir={STAGE_DIR}",
        f"/DMyOutputDir={BUILD_DIR}",
        str(ROOT / "FFXIVTracker.iss"),
    ])
    out = BUILD_DIR / f"FFXIVTracker-{version}-Setup.exe"
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build FFXIVTracker Inno Setup installer")
    parser.add_argument("--version", default=None, help="Override version tag (e.g. v1.0.0)")
    parser.add_argument("--python", default=PYTHON_DEFAULT_VERSION, metavar="VER",
                        help=f"Python embed version to bundle (default: {PYTHON_DEFAULT_VERSION})")
    parser.add_argument("--skip-python", action="store_true",
                        help="Reuse existing build/python/ — skips download + pip install")
    args = parser.parse_args()

    version = args.version or get_version()

    try:
        iscc = find_iscc()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Building FFXIVTracker-{version} installer")
    print(f"  ISCC:   {iscc}")
    print(f"  Python: {args.python}")

    if not args.skip_python:
        print("\n[1/3] Preparing Python embed ...")
        setup_python_embed(args.python)
    else:
        print("\n[1/3] Skipping Python setup (--skip-python)")

    print("\n[2/3] Staging app files ...")
    regenerate_icon()
    stage_app_files()

    print("\n[3/3] Compiling installer ...")
    out = compile_installer(version, iscc)

    size_mb = out.stat().st_size // 1024 // 1024
    print(f"\n  -> {out.relative_to(ROOT)}  ({size_mb} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
