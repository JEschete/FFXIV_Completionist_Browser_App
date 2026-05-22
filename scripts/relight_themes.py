"""Rebuild the light scheme (`colorsLight`) of a theme from its dark scheme.

Several early themes shipped with broken light schemes — flat surfaces (all
five near-identical), a collapsed text ramp (text/muted/faint nearly equal),
and todo ~= excluded. This tool regenerates `colorsLight` by taking each dark
token's HUE (which carries the game motif) and remapping lightness/saturation
to the light-mode conventions the well-authored themes (FF7/10/14/16) already
follow:

  - content `panel` is near-white; `bg` is a tinted gray; surfaces step.
  - text is a dark 3-step ramp (text -> muted -> faint).
  - `excluded` is a light, desaturated gray, clearly lighter than `todo`.
  - accents / gold / crystals / states are darkened + saturated to read on white.

Dark `colors` and `meta` are never touched, and existing `desc` strings are
preserved — only light token *values* change. Runs only on ALLOWLIST so the
hand-tuned light themes are never clobbered.

    python scripts/relight_themes.py            # rewrite allowlisted files
    python scripts/relight_themes.py --check     # report, write nothing
"""

from __future__ import annotations

import colorsys
import json
import sys
from pathlib import Path

THEMES_DIR = Path(__file__).resolve().parent.parent / "app" / "themes"

# Only these had broken light schemes (verified via hierarchy diagnostic).
ALLOWLIST = {
    "aetherial-dark.json",
    "template.json",
    "ff1-theme.json",
    "ff1-crystal-theme.json",
    "ff2-theme.json",
    "ff3-theme.json",
    "ff4-theme.json",
    "ff5-theme.json",
    "ff6-theme.json",
}

# Per-token light-mode targets, derived from the good themes' conventions.
# value = (target_lightness, saturation_rule). Rule: ("cap", x) | ("floor", x) | ("set", x).
SURFACE_TARGETS = {
    "bg":      (0.955, ("cap", 0.16)),
    "bg-soft": (0.915, ("cap", 0.16)),
    "panel":   (0.995, ("cap", 0.06)),
    "panel-2": (0.968, ("cap", 0.09)),
    "panel-3": (0.938, ("cap", 0.12)),
}
LINE_TARGETS = {
    "line":      (0.40, ("floor", 0.40)),
    "line-soft": (0.70, ("cap", 0.35)),
}
TEXT_TARGETS = {  # hue taken from the dark bg for a cohesive tint
    "text":  (0.18, ("set", 0.38)),
    "muted": (0.36, ("set", 0.28)),
    "faint": (0.52, ("set", 0.20)),
}
ACCENT_TARGETS = {
    "accent":    (0.45, ("floor", 0.58)),
    "accent-dk": (0.36, ("floor", 0.58)),
    "gold":      (0.42, ("floor", 0.72)),
}
STATE_TARGETS = {
    "done":     (0.40, ("floor", 0.55)),
    "done-dk":  (0.32, ("floor", 0.52)),
    "todo":     (0.46, ("cap", 0.34)),
    "excluded": (0.63, ("cap", 0.20)),
    "danger":   (0.45, ("floor", 0.62)),
}
CRYSTAL_TARGET = (0.47, ("floor", 0.55))
LINE_SAT_CEIL = 0.60  # keep borders from going neon


def hex_to_hls(value: str) -> tuple[float, float, float]:
    s = value.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    r, g, b = (int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)  # (h, l, s)


def hls_to_hex(h: float, l: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def apply_sat(source_s: float, rule: tuple[str, float]) -> float:
    kind, x = rule
    if kind == "cap":
        return min(source_s, x)
    if kind == "floor":
        return min(LINE_SAT_CEIL, max(source_s, x)) if x == 0.40 else max(source_s, x)
    return x  # "set"


def relight_token(dark_hex: str, target: tuple[float, tuple[str, float]],
                  hue_override: float | None = None) -> str:
    h, _l, s = hex_to_hls(dark_hex)
    if hue_override is not None:
        h = hue_override
    target_l, sat_rule = target
    return hls_to_hex(h, target_l, apply_sat(s, sat_rule))


def relight_scheme(dark: dict, light: dict) -> int:
    """Mutate `light` values from `dark` hues. Returns count of tokens changed."""
    # Tint hue for text: prefer whichever of dark bg / dark accent is actually
    # saturated. A pure-black bg (NES themes) has no real hue and colorsys
    # reports 0 (red), so falling back to the accent keeps the tint on-motif.
    def _hue_sat(group: str, key: str) -> tuple[float, float] | None:
        grp = dark.get(group, {})
        item = grp.get(key) if isinstance(grp, dict) else None
        if isinstance(item, dict) and item.get("value"):
            h, _l, s = hex_to_hls(item["value"])
            return h, s
        return None

    candidates = [c for c in (_hue_sat("surfaces", "bg"), _hue_sat("accents", "accent")) if c]
    bg_hue = max(candidates, key=lambda c: c[1])[0] if candidates else None

    group_targets = {
        "surfaces": SURFACE_TARGETS,
        "lines": LINE_TARGETS,
        "text": TEXT_TARGETS,
        "accents": ACCENT_TARGETS,
        "states": STATE_TARGETS,
    }

    changed = 0
    for group, entries in light.items():
        if not isinstance(entries, dict):
            continue
        dark_group = dark.get(group, {})
        for key, item in entries.items():
            if not isinstance(item, dict):
                continue
            dark_item = dark_group.get(key) if isinstance(dark_group, dict) else None
            dark_hex = dark_item.get("value") if isinstance(dark_item, dict) else None
            if not dark_hex:
                continue

            if group == "crystals":
                target = CRYSTAL_TARGET
            else:
                target = group_targets.get(group, {}).get(key)
            if target is None:
                continue

            hue_override = bg_hue if group == "text" else None
            new_value = relight_token(dark_hex, target, hue_override)
            if new_value != item.get("value"):
                item["value"] = new_value
                changed += 1
    return changed


def main() -> None:
    check_only = "--check" in sys.argv
    for path in sorted(THEMES_DIR.glob("*.json")):
        if path.name not in ALLOWLIST:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        dark = data.get("colors")
        light = data.get("colorsLight")
        if not isinstance(dark, dict) or not isinstance(light, dict):
            print(f"{path.name}: no dual scheme, skipped")
            continue

        changed = relight_scheme(dark, light)
        if check_only:
            print(f"{path.name}: would change {changed} light tokens")
            continue
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{path.name}: rewrote {changed} light tokens")


if __name__ == "__main__":
    main()
