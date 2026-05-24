"""Phase 4 — theme catalog integrity.

There are ~30 theme JSON files; a single malformed one would silently break
the app's theming. These tests assert every file is parseable, passes the
app's own validator, and that the catalog exposes a usable default.
"""
from __future__ import annotations

import json

import pytest

import app.main as main

THEME_FILES = sorted(main.THEMES_DIR.glob("*.json"))


def test_theme_dir_has_files():
    assert THEME_FILES, "no theme files found under app/themes"


@pytest.mark.parametrize("path", THEME_FILES, ids=lambda p: p.name)
def test_theme_file_is_valid(path):
    raw = json.loads(path.read_text(encoding="utf-8"))  # raises on bad JSON
    assert isinstance(raw, dict)
    entry = main._validate_theme(path, raw)
    assert entry["valid"], f"{path.name} invalid: {entry['errors']}"
    # both required schemes present with the required token set
    assert {"dark", "light"} <= set(entry["schemes"].keys())


def test_catalog_has_no_invalid_themes_and_a_default():
    catalog = main.get_theme_catalog()
    assert catalog["invalid"] == []
    assert catalog["default_theme_id"]
    ids = {t["id"] for t in catalog["themes"]}
    assert catalog["default_theme_id"] in ids
