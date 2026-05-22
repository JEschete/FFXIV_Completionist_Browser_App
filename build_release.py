"""
Build a release zip of FFXIVTracker.

Usage:
    python build_release.py [--version v1.0.0]

If --version is omitted, version is derived from the current git tag.
Output: dist/FFXIVTracker-<version>.zip
"""

import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# Top-level files to include verbatim
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

# Directories: omit include_only to copy everything (minus excludes)
INCLUDE_DIRS = {
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
        "include_only": {"prep_xlsx_to_sqlite.py"},
    },
    "assets": {
        "include_only": {"icon.png", "icon.ico"},
    },
}


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


def is_excluded(rel: Path, exclude: list[str]) -> bool:
    for part in rel.parts:
        if any(Path(part).match(p) for p in exclude):
            return True
    return False


def stage_dir(src: Path, dst: Path, include_only: set[str] | None, exclude: list[str]):
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        if include_only is not None and rel.parts[0] not in include_only:
            continue
        if exclude and is_excluded(rel, exclude):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def build(version: str):
    dist = ROOT / "dist"
    staging = dist / f"FFXIVTracker-{version}"
    zip_path = dist / f"FFXIVTracker-{version}.zip"

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    for name in INCLUDE_FILES:
        src = ROOT / name
        if not src.exists():
            print(f"  WARN  {name} not found, skipping")
            continue
        shutil.copy2(src, staging / name)
        print(f"  +  {name}")

    for dir_name, opts in INCLUDE_DIRS.items():
        src = ROOT / dir_name
        if not src.exists():
            print(f"  WARN  {dir_name}/ not found, skipping")
            continue
        stage_dir(
            src,
            staging / dir_name,
            include_only=opts.get("include_only"),
            exclude=opts.get("exclude", []),
        )
        print(f"  +  {dir_name}/")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in staging.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(dist))

    shutil.rmtree(staging)
    print(f"\n  -> {zip_path.relative_to(ROOT)}  ({zip_path.stat().st_size // 1024} KB)")


def main():
    parser = argparse.ArgumentParser(description="Build FFXIVTracker release zip")
    parser.add_argument("--version", default=None, help="Override version (e.g. v1.0.0)")
    args = parser.parse_args()

    version = args.version or get_version()
    print(f"Building FFXIVTracker-{version} ...")
    build(version)


if __name__ == "__main__":
    main()
