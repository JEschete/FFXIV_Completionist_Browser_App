"""Visual previewer for the JSON color themes in app/themes/.

Run it, pick a theme from the dropdown, and see:
    - a top radio toggle to switch between dark and light schemes,
  - the full meta block (name, id, scheme, version, source, and the prose
    fields like tokenConvention / notExportedYet),
  - a small mock of the app UI built from the theme's semantic tokens,
  - every color grouped exactly as the JSON defines it, and
  - the generated `:root { --token: value; }` CSS the theme would ingest into,
    with a copy button.

Pure stdlib (Tkinter) — no dependencies, no project imports.

    python scripts/preview_themes.py
"""

from __future__ import annotations

import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import ttk

THEMES_DIR = Path(__file__).resolve().parent.parent / "app" / "themes"
TEXTURES_DIR = THEMES_DIR / "textures"
TEXTURE_EXTENSIONS = (".png", ".gif", ".ppm", ".pgm")

# Meta keys shown inline on the subtitle line; everything else is rendered as a
# wrapped note so unknown/future fields surface automatically.
HEADLINE_META = ("id", "defaultScheme", "version")
FF_NUMBER_RE = re.compile(r"(?:^|[^a-z0-9])ff\s*0*(\d{1,2})(?:[^0-9]|$)", re.IGNORECASE)
POKEMON_NAME_RE = re.compile(
    r"pokemon[-_\s]*(red|blue|yellow|gold|silver|crystal|ruby|sapphire|emerald|diamond|pearl|platinum)",
    re.IGNORECASE,
)
POKEMON_THEME_ORDER: dict[str, tuple[int, int]] = {
    # Gen 1: RBY
    "red": (1, 1),
    "blue": (1, 2),
    "yellow": (1, 3),
    # Gen 2: GSC
    "gold": (2, 1),
    "silver": (2, 2),
    "crystal": (2, 3),
    # Gen 3: RSE
    "ruby": (3, 1),
    "sapphire": (3, 2),
    "emerald": (3, 3),
    # Gen 4: DPPL
    "diamond": (4, 1),
    "pearl": (4, 2),
    "platinum": (4, 3),
}


# --- theme loading ----------------------------------------------------------

def normalize_groups(colors: object) -> dict[str, list[tuple[str, str, str]]]:
    """Normalize a color block to {group: [(token, value, desc), ...]}.

    Supports both grouped entries ({"surfaces": {...}}) and flat entries where
    values are direct color dicts ({"bg": {"value": "#..."}}).
    """
    groups: dict[str, list[tuple[str, str, str]]] = {}
    if not isinstance(colors, dict):
        return groups

    for group_name, entries in colors.items():
        if isinstance(entries, dict) and isinstance(entries.get("value"), str):
            groups.setdefault("colors", []).append(
                (str(group_name), entries["value"], str(entries.get("desc", "")))
            )
            continue
        if not isinstance(entries, dict):
            continue

        bucket = groups.setdefault(str(group_name), [])
        for key, item in entries.items():
            if isinstance(item, dict):
                value = str(item.get("value", ""))
                desc = str(item.get("desc", ""))
            else:
                value, desc = str(item), ""
            if value:
                bucket.append((str(key), value, desc))

    return groups


def _looks_like_scheme_map(colors: dict) -> bool:
    """Return True when `colors` appears to be {"dark": {...}, "light": {...}}."""
    if not colors:
        return False
    keys = {str(k).lower() for k in colors}
    if not keys.issubset({"dark", "light"}):
        return False
    return all(isinstance(v, dict) for v in colors.values())


def _extract_ff_number(*values: object) -> int | None:
    for value in values:
        if value is None:
            continue
        match = FF_NUMBER_RE.search(str(value))
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _extract_pokemon_order(*values: object) -> tuple[int, int, str] | None:
    for value in values:
        if value is None:
            continue
        match = POKEMON_NAME_RE.search(str(value))
        if not match:
            continue
        token = match.group(1).lower()
        order = POKEMON_THEME_ORDER.get(token)
        if order is not None:
            return order[0], order[1], token
    return None


def _theme_sort_key(theme: dict) -> tuple[int, int, str, str]:
    """Sort FF numerically, Pokemon by gen/acronym order, others alphabetically."""
    meta = theme.get("meta") if isinstance(theme, dict) else {}
    if not isinstance(meta, dict):
        meta = {}

    path_obj = theme.get("path")
    stem = path_obj.stem if isinstance(path_obj, Path) else ""
    name = str(theme.get("name") or stem)
    theme_id = str(meta.get("id") or "")

    ff_number = _extract_ff_number(theme_id, stem, name)
    if ff_number is not None:
        # Keep variants within a numbered entry grouped and predictable.
        variant = theme_id.lower() or stem.lower() or name.lower()
        return (0, ff_number, variant, name.lower())

    pokemon_order = _extract_pokemon_order(theme_id, stem, name)
    if pokemon_order is not None:
        generation, position, token = pokemon_order
        # Enforce generation + acronym ordering (RBY, GSC, RSE, DPPL).
        return (1, generation * 10 + position, token, name.lower())

    if stem.lower() == "template":
        return (3, 9999, stem.lower(), name.lower())

    label = (theme_id or stem or name).lower()
    return (2, 9999, label, name.lower())


def load_themes() -> list[dict]:
    """Return [{path, name, meta, schemes, default_scheme}] for every theme file.

    Supported schema variants:
      - legacy:   {"colors": {...groups...}} (dark-only)
      - dual:     {"colors": {...dark...}, "colorsLight": {...light...}}
      - explicit: {"schemes": {"dark": {"colors": ...}, "light": ...}}
    """
    themes: list[dict] = []
    for path in THEMES_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Skipping {path.name}: {exc}")
            continue

        meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
        schemes: dict[str, dict[str, list[tuple[str, str, str]]]] = {}

        if isinstance(raw, dict):
            colors = raw.get("colors", {})
            if isinstance(colors, dict):
                if _looks_like_scheme_map(colors):
                    for scheme_name, block in colors.items():
                        groups = normalize_groups(block)
                        if groups:
                            schemes[str(scheme_name).lower()] = groups
                else:
                    groups = normalize_groups(colors)
                    if groups:
                        schemes["dark"] = groups

            colors_light = raw.get("colorsLight", {})
            if isinstance(colors_light, dict):
                groups = normalize_groups(colors_light)
                if groups:
                    schemes["light"] = groups

            raw_schemes = raw.get("schemes", {})
            if isinstance(raw_schemes, dict):
                for scheme_name, scheme_block in raw_schemes.items():
                    if not isinstance(scheme_block, dict):
                        continue
                    block = scheme_block.get("colors", scheme_block)
                    groups = normalize_groups(block)
                    if groups:
                        schemes[str(scheme_name).lower()] = groups

        if not schemes:
            schemes["dark"] = {}

        default_scheme = str(
            meta.get("defaultScheme") or meta.get("colorScheme") or "dark"
        ).lower()
        if default_scheme not in schemes:
            default_scheme = "dark" if "dark" in schemes else next(iter(schemes))

        themes.append({
            "path": path,
            "name": meta.get("name") or path.stem,
            "meta": meta,
            "schemes": schemes,
            "default_scheme": default_scheme,
            "available_schemes": tuple(schemes.keys()),
        })
    themes.sort(key=_theme_sort_key)
    return themes


def flat_tokens(groups: dict) -> dict[str, str]:
    """Flatten all groups into a {key: value} lookup for the mock UI."""
    tokens: dict[str, str] = {}
    for entries in groups.values():
        for key, value, _desc in entries:
            tokens[key] = value
    return tokens


def build_css(groups: dict) -> str:
    """Render the `:root { --token: value; }` block a theme would ingest into."""
    lines = [":root {"]
    for group_name, entries in groups.items():
        lines.append(f"  /* {group_name} */")
        for key, value, _desc in entries:
            lines.append(f"  --{key}: {value};")
    lines.append("}")
    return "\n".join(lines)


# --- text / color helpers ----------------------------------------------------

def humanize(key: str) -> str:
    """`notExportedYet` -> `Not Exported Yet`, `source` -> `Source`."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(key))
    return spaced[:1].upper() + spaced[1:]


def parse_hex(value: str) -> tuple[int, int, int]:
    s = value.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) < 6:
        return (0, 0, 0)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def readable_on(value: str) -> str:
    """Black or white text that stays legible on the given background."""
    r, g, b = parse_hex(value)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if luminance > 0.55 else "#ffffff"


def tok(tokens: dict[str, str], key: str, fallback: str) -> str:
    return tokens.get(key, fallback)


def _pixel_rgb(pixel: object) -> tuple[int, int, int]:
    """Normalize PhotoImage.get() output to an RGB tuple."""
    if isinstance(pixel, tuple) and len(pixel) >= 3:
        return int(pixel[0]), int(pixel[1]), int(pixel[2])
    if isinstance(pixel, str):
        s = pixel.strip()
        if s.startswith("#"):
            return parse_hex(s)
        if s.startswith("(") and s.endswith(")"):
            parts = [p.strip() for p in s[1:-1].split(",")]
            if len(parts) >= 3:
                try:
                    return int(parts[0]), int(parts[1]), int(parts[2])
                except ValueError:
                    pass
    return (0, 0, 0)


def load_texture_image(master: tk.Misc) -> tuple[tk.PhotoImage | None, Path | None]:
    """Load the first supported texture image under app/themes/textures/."""
    if not TEXTURES_DIR.exists():
        return None, None

    candidates = sorted(
        p for p in TEXTURES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in TEXTURE_EXTENSIONS
    )
    for path in candidates:
        try:
            return tk.PhotoImage(master=master, file=str(path)), path
        except tk.TclError:
            continue
    return None, None


def prepare_texture_tile(image: tk.PhotoImage, max_side: int = 224) -> tk.PhotoImage:
    """Downsample large source images to a practical tile size for fast tinting."""
    w = max(1, image.width())
    h = max(1, image.height())
    sx = max(1, (w + max_side - 1) // max_side)
    sy = max(1, (h + max_side - 1) // max_side)
    if sx == 1 and sy == 1:
        return image
    return image.subsample(sx, sy)  # type: ignore[arg-type]


def tint_grayscale_texture(
    master: tk.Misc,
    source: tk.PhotoImage,
    tint_hex: str,
    *,
    strength: float = 0.62,
    shadow_floor: float = 0.08,
) -> tk.PhotoImage:
    """Tint a grayscale texture using a theme color while preserving detail.

    shadow_floor lifts near-black pixels so mortar lines can still pick up
    slight color influence instead of remaining pure black.
    """
    w = max(1, source.width())
    h = max(1, source.height())
    tr, tg, tb = parse_hex(tint_hex)

    floor = max(0.0, min(0.4, float(shadow_floor)))
    amt = max(0.0, min(1.0, float(strength)))

    out = tk.PhotoImage(master=master, width=w, height=h)
    for y in range(h):
        row_colors: list[str] = []
        for x in range(w):
            pr, pg, pb = _pixel_rgb(source.get(x, y))
            lum = (0.299 * pr + 0.587 * pg + 0.114 * pb) / 255.0
            lum = floor + (1.0 - floor) * lum

            base_gray = int(round(lum * 255.0))
            tint_r = int(round(lum * tr))
            tint_g = int(round(lum * tg))
            tint_b = int(round(lum * tb))

            rr = int(round((1.0 - amt) * base_gray + amt * tint_r))
            gg = int(round((1.0 - amt) * base_gray + amt * tint_g))
            bb = int(round((1.0 - amt) * base_gray + amt * tint_b))
            row_colors.append(f"#{rr:02x}{gg:02x}{bb:02x}")

        out.put("{" + " ".join(row_colors) + "}", to=(0, y))
    return out


# --- UI ----------------------------------------------------------------------

class ScrollFrame(ttk.Frame):
    """A vertically scrollable container; put content in `self.body`.

    Labels registered via `register_wrap` get their wraplength kept in sync with
    the viewport width so prose meta fields reflow instead of clipping.
    """

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self._bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._bar.set)
        self._bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self._canvas, bd=0, highlightthickness=0)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self._wrap_targets: list[tuple[tk.Label, int]] = []

        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_body(self, _event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event):
        self._canvas.itemconfigure(self._window, width=event.width)
        for label, margin in self._wrap_targets:
            try:
                label.configure(wraplength=max(120, event.width - margin))
            except tk.TclError:
                pass

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def set_bg(self, color: str):
        self._canvas.configure(bg=color)
        self.body.configure(bg=color)

    def clear_wrap(self):
        self._wrap_targets = []

    def register_wrap(self, label: tk.Label, margin: int = 70):
        self._wrap_targets.append((label, margin))
        width = self._canvas.winfo_width()
        if width > 1:
            label.configure(wraplength=max(120, width - margin))


class ThemePreview(tk.Tk):
    def __init__(self, themes: list[dict]):
        super().__init__()
        self.title("FFXIV Tracker — Theme Preview")
        self.geometry("880x760")
        self.minsize(560, 480)
        self.themes = themes
        self._by_name = {t["name"]: t for t in themes}
        self._texture_source_raw, self._texture_source_path = load_texture_image(self)
        self._texture_tile = (
            prepare_texture_tile(self._texture_source_raw)
            if self._texture_source_raw is not None else None
        )
        self._tinted_texture_cache: dict[str, tk.PhotoImage] = {}

        picker = ttk.Frame(self, padding=10)
        picker.pack(side="top", fill="x")
        self.scheme_var = tk.StringVar(value="dark")
        ttk.Label(picker, text="Mode:").pack(side="left")
        ttk.Radiobutton(
            picker,
            text="Dark",
            value="dark",
            variable=self.scheme_var,
            command=self.render,
        ).pack(side="left", padx=(4, 0))
        ttk.Radiobutton(
            picker,
            text="Light",
            value="light",
            variable=self.scheme_var,
            command=self.render,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(picker, text="Theme:").pack(side="left")
        self.combo = ttk.Combobox(
            picker, state="readonly", width=46,
            values=[t["name"] for t in themes],
        )
        self.combo.pack(side="left", padx=8)
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self.render())
        ttk.Button(picker, text="Refresh", command=self.refresh_themes).pack(side="left")
        self.status = ttk.Label(picker, text="")
        self.status.pack(side="left", padx=10)

        self.scroll = ScrollFrame(self)
        self.scroll.pack(side="top", fill="both", expand=True)

        if themes:
            self.combo.current(0)
            self.render()
        else:
            self._render_empty_state(f"No themes found in {THEMES_DIR}")

    def current(self) -> dict | None:
        return self._by_name.get(self.combo.get())

    def _groups_for_selected_scheme(
        self,
        theme: dict,
    ) -> tuple[str, dict[str, list[tuple[str, str, str]]]]:
        schemes = theme.get("schemes", {})
        if not isinstance(schemes, dict) or not schemes:
            return "dark", {}

        selected = self.scheme_var.get().strip().lower() or "dark"
        if selected in schemes:
            return selected, schemes[selected]

        fallback = str(theme.get("default_scheme") or "dark").lower()
        if fallback in schemes:
            return fallback, schemes[fallback]

        first = next(iter(schemes))
        return first, schemes[first]

    def render(self):
        theme = self.current()
        if theme is None:
            self._render_empty_state(f"No themes found in {THEMES_DIR}")
            return
        self.scroll.clear_wrap()
        for child in self.scroll.body.winfo_children():
            child.destroy()

        active_scheme, groups = self._groups_for_selected_scheme(theme)
        tokens = flat_tokens(groups)
        bg = tok(tokens, "bg", "#0b0e15")
        text = tok(tokens, "text", "#e7ecf5")
        self.scroll.set_bg(bg)

        self._render_meta(theme, active_scheme, bg, text, tokens)
        self._render_texture_preview(bg, text, tokens)
        self._render_mock(tokens, bg, text)
        self._render_swatches(groups, bg, text, tokens)
        self._render_css_export(groups, bg, text, tokens)

    def _texture_for_theme(self, tokens: dict[str, str]) -> tk.PhotoImage | None:
        if self._texture_tile is None:
            return None
        tint_key = tok(tokens, "accent", "#6ba4e8").strip().lower()
        if tint_key in self._tinted_texture_cache:
            return self._tinted_texture_cache[tint_key]
        tinted = tint_grayscale_texture(self, self._texture_tile, tint_key)
        self._tinted_texture_cache[tint_key] = tinted
        return tinted

    def _paint_tiled_texture(self, canvas: tk.Canvas, image: tk.PhotoImage):
        canvas.delete("texture")
        w = max(1, canvas.winfo_width())
        h = max(1, canvas.winfo_height())
        tw = max(1, image.width())
        th = max(1, image.height())
        for y in range(0, h, th):
            for x in range(0, w, tw):
                canvas.create_image(x, y, image=image, anchor="nw", tags="texture")
        canvas.lower("texture")

    def _render_texture_preview(self, bg: str, text: str, tokens: dict[str, str]):
        muted = tok(tokens, "muted", "#8995a8")
        line = tok(tokens, "line", "#2a3445")
        panel = tok(tokens, "panel", "#161c28")
        accent = tok(tokens, "accent", "#6ba4e8")

        wrap = tk.Frame(self.scroll.body, bg=bg)
        wrap.pack(fill="x", padx=18, pady=(8, 0))
        tk.Label(
            wrap,
            text="TEXTURED BACKGROUND PREVIEW",
            bg=bg,
            fg=muted,
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        border = tk.Frame(wrap, bg=line, padx=1, pady=1)
        border.pack(fill="x")
        canvas = tk.Canvas(
            border,
            height=140,
            bg=panel,
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="x")

        image = self._texture_for_theme(tokens)
        if image is None:
            canvas.create_text(
                14,
                24,
                anchor="nw",
                fill=text,
                text=f"No supported texture found in {TEXTURES_DIR}",
                font=("Segoe UI", 10),
            )
            return

        def _repaint(_event=None):
            self._paint_tiled_texture(canvas, image)
            canvas.create_text(
                14,
                14,
                anchor="nw",
                fill=readable_on(accent),
                text=f"Tint: {accent}",
                font=("Consolas", 10, "bold"),
                tags="texture",
            )
            if self._texture_source_path is not None:
                canvas.create_text(
                    14,
                    34,
                    anchor="nw",
                    fill=readable_on(accent),
                    text=self._texture_source_path.name,
                    font=("Consolas", 8),
                    tags="texture",
                )

        canvas.bind("<Configure>", _repaint)
        _repaint()

    def _render_empty_state(self, message: str):
        self.scroll.clear_wrap()
        for child in self.scroll.body.winfo_children():
            child.destroy()
        self.scroll.set_bg("#0b0e15")
        tk.Label(
            self.scroll.body,
            text=message,
            bg="#0b0e15",
            fg="#e7ecf5",
            font=("Segoe UI", 11),
        ).pack(pady=40)

    def refresh_themes(self):
        selected_name = self.combo.get().strip()
        self.themes = load_themes()
        self._by_name = {t["name"]: t for t in self.themes}

        names = [t["name"] for t in self.themes]
        self.combo.configure(values=names)
        if not names:
            self.combo.set("")
            self._render_empty_state(f"No themes found in {THEMES_DIR}")
            self.status.configure(text="No themes loaded")
            self.after(2000, lambda: self.status.configure(text=""))
            return

        if selected_name and selected_name in self._by_name:
            self.combo.set(selected_name)
        else:
            self.combo.current(0)

        self.render()
        count = len(names)
        suffix = "" if count == 1 else "s"
        self.status.configure(text=f"Reloaded {count} theme{suffix}")
        self.after(2000, lambda: self.status.configure(text=""))

    # -- sections ----------------------------------------------------------

    def _render_meta(self, theme, active_scheme, bg, text, tokens):
        meta = theme["meta"]
        muted = tok(tokens, "muted", "#8995a8")
        faint = tok(tokens, "faint", "#5d6a7e")
        line = tok(tokens, "line", "#2a3445")
        panel = tok(tokens, "panel", "#161c28")

        wrap = tk.Frame(self.scroll.body, bg=bg)
        wrap.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(
            wrap, text=meta.get("name", theme["name"]), bg=bg, fg=text,
            font=("Segoe UI", 16, "bold"), anchor="w",
        ).pack(fill="x")

        sub_parts = [str(meta[k]) for k in HEADLINE_META if meta.get(k) is not None]
        sub_parts.append(f"scheme:{active_scheme}")
        available = theme.get("available_schemes", ())
        if isinstance(available, tuple) and len(available) > 1:
            sub_parts.append(f"available:{'/'.join(available)}")
        sub = " · ".join(sub_parts)
        if sub:
            tk.Label(
                wrap, text=sub, bg=bg, fg=muted, font=("Segoe UI", 9), anchor="w",
            ).pack(fill="x")
        tk.Label(
            wrap, text=theme["path"].name, bg=bg, fg=faint,
            font=("Consolas", 8), anchor="w",
        ).pack(fill="x")

        # Every remaining meta key (source, tokenConvention, notExportedYet, …)
        # rendered as a wrapped note so nothing in the format is dropped.
        note_keys = [k for k in meta if k != "name" and k not in HEADLINE_META]
        if not note_keys:
            return
        border = tk.Frame(self.scroll.body, bg=line, padx=1, pady=1)
        border.pack(fill="x", padx=18, pady=(8, 4))
        notes = tk.Frame(border, bg=panel)
        notes.pack(fill="both", expand=True)
        for k in note_keys:
            tk.Label(
                notes, text=humanize(k), bg=panel, fg=tok(tokens, "gold", "#e2bd72"),
                font=("Segoe UI", 8, "bold"), anchor="w",
            ).pack(fill="x", padx=10, pady=(8, 0))
            value_lbl = tk.Label(
                notes, text=str(meta[k]), bg=panel, fg=muted,
                font=("Segoe UI", 9), anchor="w", justify="left",
            )
            value_lbl.pack(fill="x", padx=10, pady=(0, 6))
            self.scroll.register_wrap(value_lbl, margin=80)

    def _render_mock(self, tokens, bg, text):
        """A tiny mock of the real UI built from semantic tokens."""
        gold = tok(tokens, "gold", "#e2bd72")
        accent = tok(tokens, "accent", "#6ba4e8")
        panel = tok(tokens, "panel", "#161c28")
        bg_soft = tok(tokens, "bg-soft", "#10151f")
        line = tok(tokens, "line", "#2a3445")
        muted = tok(tokens, "muted", "#8995a8")

        outer = tk.Frame(self.scroll.body, bg=line, padx=1, pady=1)
        outer.pack(fill="x", padx=18, pady=10)
        card = tk.Frame(outer, bg=bg)
        card.pack(fill="both", expand=True)

        # header bar with accent pill
        header = tk.Frame(card, bg=bg_soft)
        header.pack(fill="x")
        tk.Label(
            header, text="FFXIV Completion Tracker", bg=bg_soft, fg=text,
            font=("Segoe UI", 11, "bold"), padx=12, pady=10,
        ).pack(side="left")
        tk.Label(
            header, text="Sort", bg=accent, fg=readable_on(accent),
            font=("Segoe UI", 9, "bold"), padx=12, pady=4,
        ).pack(side="right", padx=12)

        # section title + a panel with status chips
        tk.Label(
            card, text="A REALM REBORN", bg=bg, fg=gold,
            font=("Segoe UI", 9, "bold"), anchor="w", padx=12,
        ).pack(fill="x", pady=(10, 2))

        panel_box = tk.Frame(card, bg=panel)
        panel_box.pack(fill="x", padx=12, pady=(0, 12))
        chips = tk.Frame(panel_box, bg=panel)
        chips.pack(fill="x", padx=10, pady=10)
        for label, key, fallback in (
            ("Done", "done", "#45c78a"),
            ("To Do", "todo", "#586374"),
            ("Excluded", "excluded", "#4a4f5c"),
            ("Danger", "danger", "#d96b6b"),
        ):
            color = tok(tokens, key, fallback)
            tk.Label(
                chips, text=label, bg=color, fg=readable_on(color),
                font=("Segoe UI", 9, "bold"), padx=12, pady=5,
            ).pack(side="left", padx=(0, 8))
        tk.Label(
            panel_box, text="Secondary / muted caption text", bg=panel, fg=muted,
            font=("Segoe UI", 9), anchor="w", padx=10,
        ).pack(fill="x", pady=(0, 10))

    def _render_swatches(self, groups, bg, text, tokens):
        muted = tok(tokens, "muted", "#8995a8")
        for group_name, entries in groups.items():
            tk.Label(
                self.scroll.body, text=group_name.upper(), bg=bg, fg=muted,
                font=("Segoe UI", 9, "bold"), anchor="w",
            ).pack(fill="x", padx=18, pady=(12, 4))

            for key, value, desc in entries:
                row = tk.Frame(self.scroll.body, bg=bg)
                row.pack(fill="x", padx=18, pady=2)

                swatch = tk.Label(
                    row, text=value, bg=value, fg=readable_on(value),
                    font=("Consolas", 9, "bold"), width=12, height=2,
                    relief="flat", bd=0,
                )
                swatch.pack(side="left")

                info = tk.Frame(row, bg=bg)
                info.pack(side="left", fill="x", expand=True, padx=10)
                tk.Label(
                    info, text=f"--{key}", bg=bg, fg=text,
                    font=("Consolas", 10, "bold"), anchor="w",
                ).pack(fill="x")
                if desc:
                    desc_lbl = tk.Label(
                        info, text=desc, bg=bg, fg=muted,
                        font=("Segoe UI", 9), anchor="w", justify="left",
                    )
                    desc_lbl.pack(fill="x")
                    self.scroll.register_wrap(desc_lbl, margin=190)

    def _render_css_export(self, groups, bg, text, tokens):
        muted = tok(tokens, "muted", "#8995a8")
        line = tok(tokens, "line", "#2a3445")
        panel = tok(tokens, "panel", "#161c28")
        css = build_css(groups)

        head = tk.Frame(self.scroll.body, bg=bg)
        head.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(
            head, text="GENERATED CSS  ( :root )", bg=bg, fg=muted,
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(side="left")
        ttk.Button(
            head, text="Copy", width=8,
            command=lambda: self._copy(css),
        ).pack(side="right")

        box = tk.Text(
            self.scroll.body, height=css.count("\n") + 1, wrap="none",
            bg=panel, fg=text, insertbackground=text, font=("Consolas", 10),
            bd=0, highlightthickness=1, highlightbackground=line,
            highlightcolor=line, padx=10, pady=8,
        )
        box.insert("1.0", css)
        box.configure(state="disabled")
        box.pack(fill="x", padx=18, pady=(0, 24))

    def _copy(self, css: str):
        self.clipboard_clear()
        self.clipboard_append(css)
        self.status.configure(text="CSS copied to clipboard")
        self.after(2000, lambda: self.status.configure(text=""))


def main() -> None:
    themes = load_themes()
    ThemePreview(themes).mainloop()


if __name__ == "__main__":
    main()
