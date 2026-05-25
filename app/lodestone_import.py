from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import sqlite3
import traceback
import unicodedata
import urllib.request
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from app import db, progress_io

_STATIC_AVATARS = Path(__file__).parent / "static" / "avatars"


def _save_avatar(payload: dict[str, Any], character_id: int, log: Callable[[str], None]) -> None:
    avatar_url = (payload.get("profile") or {}).get("images", {}).get("avatar")
    if not avatar_url:
        return
    dest = _STATIC_AVATARS / f"{character_id}.jpg"
    if dest.exists():
        return
    try:
        _STATIC_AVATARS.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(avatar_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest.write_bytes(resp.read())
        log(f"Avatar saved for character {character_id}")
    except Exception as exc:
        log(f"Avatar download skipped: {exc}")


_COMMON_CONFUSABLES = str.maketrans({
    "§": "s",
    "Α": "A",
    "α": "a",
    "Ι": "I",
    "ι": "i",
    "Æ": "AE",
    "æ": "ae",
    "Œ": "OE",
    "œ": "oe",
    "’": "'",
})


@dataclass
class ImportSummary:
    character_id: int
    character_name: str
    source_path: str
    run_id: int
    total_candidates: int
    matched_candidates: int
    unmatched_candidates: int
    rows_applied: int
    rows_skipped_already_done: int
    unmatched_items: list[dict[str, Any]]


LODESTONE_LEVEL_MERGE_MODES = {"keep-highest", "overwrite"}


def load_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Lodestone payload must be a JSON object")
    return data


def _norm_label(value: str) -> str:
    value = value.translate(_COMMON_CONFUSABLES)
    folded = unicodedata.normalize("NFKD", value).casefold()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    # Treat apostrophes as optional (Vana'diel vs Vanadiel, etc.).
    folded = folded.replace("'", "")
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return folded.strip()


def _strip_marker_prefixes(value: str) -> str:
    # Lodestone sometimes prefixes quest names with icon glyphs or bullets.
    text = value.strip()
    while text and not (text[0].isalnum() or text[0] in {"'", '"'}):
        text = text[1:]
    return text.strip()


_QUEST_LABEL_RENAMES_BY_NORM = {
    _norm_label("Crossing Paths"): "Crossroads",
    _norm_label("Hither and Yarns"): "Hither and Yams",
}

_CRAFTING_LABEL_RENAMES_BY_BUCKET_NORM: dict[str, dict[str, str]] = {
    "logs/crafting-log/alchemist": {
        _norm_label("Grade 3 Tincture of Dexterity"): "Grade 3 Tinctures of Dexterity",
        _norm_label("Grade 3 Tincture of Intelligence"): "Grade 3 Tinctures of Intelligence",
        _norm_label("Grade 3 Tincture of Mind"): "Grade 3 Tinctures of Mind",
        _norm_label("Grade 3 Tincture of Strength"): "Grade 3 Tinctures of Strength",
        _norm_label("Grade 3 Tincture of Vitality"): "Grade 3 Tinctures of Vitality",
        _norm_label("The Black Wolf Stalks Again Orchestrion Roll"): "The Black Wolf Strikes Again Orchestrion Roll",
        _norm_label("Wind Ward Mega-Potion"): "Wind Wand Mega-Potion",
    },
    "logs/crafting-log/blacksmith": {
        _norm_label("Dwarven Mythril War Scythe"): "Dwarven Mythril Scythe",
        _norm_label("Titanbronze Fists"): "Titanbronze Fist",
    },
    "logs/crafting-log/carpenter": {
        _norm_label("White Ash Earrings"): "White Ash Earring",
    },
    "logs/crafting-log/culinarian": {
        _norm_label("Dark Rye Flour"): "Dark Rye Flower",
    },
    "logs/crafting-log/goldsmith": {
        _norm_label("Star Quartz Wristband of Aiming"): "Star Quartz Wristband of",
        _norm_label("Star Quartz Wristband of Casting"): "Star Quartz Wristband of",
    },
    "logs/crafting-log/leatherworker": {
        _norm_label("Dalmascan Leather Shoes"): "Dalmascan Leather Boots",
        _norm_label("Rarefied Crocodileskin Leggings"): "Rarefied Crocodileskin Leggins",
    },
}

_CRAFTING_ALKAHEST_STAT_BY_AFFIX = {
    "fending": "vitality",
    "striking": "strength",
    "aiming": "dexterity",
    "casting": "intelligence",
    "healing": "mind",
}


def _quest_label_aliases(raw: str) -> set[str]:
    """Generate likely quest label forms from Lodestone payload labels.

    Example: "Abalathian Sidequests (A Cropper's Duty)" ->
      - full string
      - inner: "A Cropper's Duty"
    """
    value = _strip_marker_prefixes(raw.strip())
    aliases = {value} if value else set()

    # Pull inner quest title from category wrappers.
    m = re.match(r"^(.+?)\s*\((.+)\)\s*$", value)
    if m:
        inner = _strip_marker_prefixes(m.group(2).strip())
        if inner:
            aliases.add(inner)

    # For labels like "Role Quests (Shadowbringers) (Quest Name)", use the
    # trailing parenthetical content as the likely actual quest title.
    m_last = re.search(r"\(([^()]*)\)\s*$", value)
    if m_last:
        inner_last = _strip_marker_prefixes(m_last.group(1).strip())
        if inner_last:
            aliases.add(inner_last)

    # Remove any leading marker glyphs if still present after normalization.
    cleaned = _strip_marker_prefixes(value)
    if cleaned:
        aliases.add(cleaned)

    renamed = _QUEST_LABEL_RENAMES_BY_NORM.get(_norm_label(cleaned or value))
    if renamed:
        aliases.add(renamed)

    return {a for a in aliases if a}


def _add_candidate(pool: dict[str, set[str]], bucket: str, raw: Any) -> None:
    if not isinstance(raw, str):
        return
    value = raw.strip()
    if not value:
        return

    # Lodestone may emit unknown Triple Triad placeholders; do not treat as
    # actionable unmatched items.
    if bucket in {"tripletriad", "bluemagic"} and re.fullmatch(r"[?？]+", value):
        return

    pool.setdefault(bucket, set()).add(value)


def collect_candidates(payload: dict[str, Any]) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = {
        "quest": set(),
        "achievement": set(),
        "minion": set(),
        "mount": set(),
        "tripletriad": set(),
        "bluemagic": set(),
        "emote": set(),
        "orchestrion": set(),
    }

    minions = payload.get("minions")
    if isinstance(minions, dict):
        items = minions.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _add_candidate(candidates, "minion", item.get("name"))

    mounts = payload.get("mounts")
    if isinstance(mounts, dict):
        items = mounts.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _add_candidate(candidates, "mount", item.get("name"))

    achievements = payload.get("achievements")
    if isinstance(achievements, dict):
        entries = achievements.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    _add_candidate(candidates, "achievement", entry.get("title"))

    auth_pages = payload.get("authenticated_pages")
    if isinstance(auth_pages, dict):
        quest_page = auth_pages.get("quest")
        if isinstance(quest_page, dict):
            entries = quest_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "quest", entry.get("title"))

        triad_page = auth_pages.get("goldsaucer/tripletriad")
        if isinstance(triad_page, dict):
            entries = triad_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "tripletriad", entry.get("name"))

        bluemage_page = auth_pages.get("bluemage")
        if isinstance(bluemage_page, dict):
            entries = bluemage_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "bluemagic", entry.get("name") or entry.get("title"))

        emote_page = auth_pages.get("emote")
        if isinstance(emote_page, dict):
            entries = emote_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "emote", entry.get("name") or entry.get("label"))

        orchestrion_page = auth_pages.get("orchestrion")
        if isinstance(orchestrion_page, dict):
            entries = orchestrion_page.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        _add_candidate(candidates, "orchestrion", entry.get("name") or entry.get("label"))

    return candidates


# Story/raid/relic sheets that represent quest completion but often do not
# contain the literal word "quest" in the sheet title.
_QUEST_LIKE_SHEET_TOKENS = (
    "quest",
    "leves",
    "primals",
    "bahamut",
    "the crystal tower",
    "alexander",
    "the warring triad",
    "the shadow of mach",
    "omega",
    "return to ivalice",
    "the four lords",
    "eden",
    "yorha dark apocalypse",
    "the sorrow of werlyt",
    "pandæmonium",
    "myths of the realm",
    "the arcadion",
    "echoes of vanadiel",
    "echoes of vana'diel",
    "chronicles of light",
    "hildibrand",
    "weapon enhancement",
    "records of unusual endeavors",
    "side story quests",
    "disciple of war quests",
    "disciple of magic quests",
    "disciple of the hand quests",
    "disciple of the land quests",
    "disciple of war job quests",
    "disciple of magic job quests",
    "role quests",
    "hall of the novice",
    "crystalline mean quests",
    "studium quests",
    "wachumeqimeqi quests",
    "relic tools",
    "relic weapons",
)

_QUEST_LIKE_SHEET_TOKENS_NORM = tuple(
    sorted({norm for token in _QUEST_LIKE_SHEET_TOKENS if (norm := _norm_label(token))})
)

_SHEET_BUCKET_OVERRIDES: dict[str, frozenset[str]] = {
    _norm_label("Adventurer Plate"): frozenset({"character/adventure-plate"}),
    # Desktop adventure-plate @PORTRAIT.* decorations live in the workbook's
    # separate "Portraits" sheet; index it under the same bucket so portrait
    # backgrounds/frames/accents resolve (section filtering keeps them apart).
    _norm_label("Portraits"): frozenset({"character/adventure-plate"}),
    _norm_label("Alchemy Log"): frozenset({"logs/crafting-log/alchemist"}),
    _norm_label("Armorcrafting Log"): frozenset({"logs/crafting-log/armorer"}),
    _norm_label("Blacksmithing Log"): frozenset({"logs/crafting-log/blacksmith"}),
    _norm_label("Bozja - Aetherytes"): frozenset({"duty/exploratory-missions/bozja/aetherytes"}),
    _norm_label("Bozja - Duties"): frozenset({"duty/exploratory-missions/bozja/duties"}),
    _norm_label("Bozja - Events"): frozenset({"duty/exploratory-missions/bozja/events"}),
    _norm_label("Bozja - Lost Actions"): frozenset({"duty/exploratory-missions/bozja/lost-actions"}),
    _norm_label("Bozja - Resistance Rank"): frozenset({
        "duty/exploratory-missions/bozja/resistance-rank",
        "duty/exploratory-missions/bozja/resistance-honors",
    }),
    _norm_label("Botanist Logs"): frozenset({
        "logs/gathering/gathering-log/harvesting",
        "logs/gathering/gathering-log/logging",
    }),
    _norm_label("Carpentry Log"): frozenset({"logs/crafting-log/carpenter"}),
    _norm_label("Culinary Log"): frozenset({"logs/crafting-log/culinarian"}),
    _norm_label("Goldsmithing Log"): frozenset({"logs/crafting-log/goldsmith"}),
    _norm_label("Leatherworking Log"): frozenset({"logs/crafting-log/leatherworker"}),
    _norm_label("Miner Logs"): frozenset({
        "logs/gathering/gathering-log/mining",
        "logs/gathering/gathering-log/quarrying",
    }),
    _norm_label("Shared Craft Log"): frozenset({"logs/crafting-log/shared"}),
    _norm_label("Weaving Log"): frozenset({"logs/crafting-log/weaver"}),
    _norm_label("Triple Triad Opponents"): frozenset({"character/gold-saucer/triple-triad-opponents"}),
    _norm_label("Dungeons"): frozenset({"duty/duty-raid-finder/dungeon"}),
    _norm_label("Trials"): frozenset({"duty/duty-raid-finder/trial"}),
    _norm_label("Raids"): frozenset({"duty/duty-raid-finder/raid"}),
    _norm_label("Guildhests"): frozenset({"duty/duty-raid-finder/guildhests"}),
    _norm_label("Hall of the Novice"): frozenset({"duty/hall-of-the-novice"}),
    _norm_label("Island Sanctuary - Animals"): frozenset({"duty/island-sanctuary/animals"}),
    _norm_label("Island Sanctuary - Rare Animals"): frozenset({"duty/island-sanctuary/animals"}),
    _norm_label("Island Sanctuary - Buildings"): frozenset({"duty/island-sanctuary/buildings"}),
    _norm_label("Island Sanctuary - Crafting"): frozenset({"duty/island-sanctuary/crafting"}),
    _norm_label("Island Sanctuary - Isleventory"): frozenset({"duty/island-sanctuary/isleventory"}),
    _norm_label("Island Sanctuary - Rank"): frozenset({"duty/island-sanctuary/rank"}),
    _norm_label("Mount Speed"): frozenset({"travel/mount-speed"}),
    _norm_label("Porters"): frozenset({"travel/porters"}),
    _norm_label("Aetherytes"): frozenset({"travel/aetherytes"}),
    _norm_label("Sightseeing Logs"): frozenset({"logs/sightseeing-log"}),
    _norm_label("Fishing Logs"): frozenset({"logs/gathering/fishing-log"}),
    _norm_label("Fish Guide"): frozenset({"logs/gathering/fishing-guide"}),
}


def _guildhests_bucket_from_section(section_label: str | None) -> str | None:
    section = str(section_label or "").strip()
    if not section:
        return None
    slug = re.sub(r"\s+", "-", _norm_label(section)).strip("-")
    if not slug:
        return None
    return f"duty/duty-raid-finder/guildhests/{slug}"


_ADVENTURE_PLATE_SECTION_TAG_PREFIX = "@ADVPSEC."
_QUEST_PATH_TOKEN_TAG_PREFIX = "@QPATH."

_INTERNAL_SOURCE_LABEL_PREFIXES = (
    _ADVENTURE_PLATE_SECTION_TAG_PREFIX,
    _QUEST_PATH_TOKEN_TAG_PREFIX,
)

_QUEST_PATH_STOP_TOKENS = {
    "duty",
    "quest",
    "json",
    "index",
    "_index",
}

_STARTING_CLASS_CITY_BY_KEY = {
    "lancer": "gridania",
    "archer": "gridania",
    "conjurer": "gridania",
    "marauder": "limsa",
    "arcanist": "limsa",
    "gladiator": "uldah",
    "pugilist": "uldah",
    "thaumaturge": "uldah",
}

_STARTING_CITY_SOURCE_TOKENS = {
    "gridania": {"gridania", "gridanian", "gridaniansidequests"},
    "limsa": {"limsa", "lominsa", "limsalominsa", "lominsan", "lominsansidequests"},
    "uldah": {"uldah", "uldahn", "uldahnsidequests"},
}

# Decoration tokens are namespaced "plate.*"/"portrait.*" so that a single
# desktop item (which can grant several decoration slots) is filtered to the
# matching workbook sheet/section. The desktop "Adventure Plate" group maps to
# two workbook sheets: "Adventurer Plate" (the @PLATE.* slots) and "Portraits"
# (the @PORTRAIT.* slots). Both sheets reuse some section names (notably
# "Accent"), so keys must stay sheet-qualified to avoid cross-sheet collisions.
_ADVENTURE_PLATE_DECORATION_SECTION_KEYS = {
    "PLATE.BASE": "plate.base",
    "PLATE.BACKING": "plate.backing",
    "PLATE.TOP_BORDER": "plate.topborder",
    "PLATE.BOTTOM_BORDER": "plate.bottomborder",
    "PLATE.ACCENT": "plate.accent",
    "PLATE.FRAME": "plate.frame",
    "PLATE.PORTRAIT_FRAME": "plate.portraitframe",
    "PLATE.PATTERN": "plate.pattern",
    "PLATE.PATTERN_OVERLAY": "plate.pattern",
    "PLATE.OVERLAY": "plate.pattern",
    "PORTRAIT.BACKGROUND": "portrait.background",
    "PORTRAIT.FRAME": "portrait.frame",
    "PORTRAIT.ACCENT": "portrait.accent",
}

# Workbook (sheet, normalized section label) -> namespaced section key.
_ADVENTURE_PLATE_ROW_SECTION_KEYS = {
    ("Adventurer Plate", "baseplate"): "plate.base",
    ("Adventurer Plate", "backing"): "plate.backing",
    ("Adventurer Plate", "topborder"): "plate.topborder",
    ("Adventurer Plate", "bottomborder"): "plate.bottomborder",
    ("Adventurer Plate", "accent"): "plate.accent",
    ("Adventurer Plate", "plateframe"): "plate.frame",
    ("Adventurer Plate", "portraitframe"): "plate.portraitframe",
    ("Adventurer Plate", "patternoverlay"): "plate.pattern",
    ("Portraits", "background"): "portrait.background",
    ("Portraits", "frame"): "portrait.frame",
    ("Portraits", "accent"): "portrait.accent",
}


def _adventure_plate_row_section_key(sheet_name: Any, section_label: Any) -> str | None:
    sheet = str(sheet_name or "").strip()
    norm = _norm_lookup_key(str(section_label or ""))
    if not sheet or not norm:
        return None
    return _ADVENTURE_PLATE_ROW_SECTION_KEYS.get((sheet, norm))


def _adventure_plate_source_section_tags(item: dict[str, Any]) -> set[str]:
    decorations = item.get("decorations")
    if isinstance(decorations, str):
        tokens = [decorations]
    elif isinstance(decorations, list):
        tokens = [value for value in decorations if isinstance(value, str)]
    else:
        tokens = []

    out: set[str] = set()
    for token in tokens:
        cleaned = token.strip().lstrip("@")
        if not cleaned:
            continue
        normalized = cleaned.upper().replace("-", "_").replace(" ", "_")
        section_key = _ADVENTURE_PLATE_DECORATION_SECTION_KEYS.get(normalized)
        if section_key:
            out.add(f"{_ADVENTURE_PLATE_SECTION_TAG_PREFIX}{section_key}")
    return out


def _adventure_plate_sections_from_labels(labels: list[str] | tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    prefix = _ADVENTURE_PLATE_SECTION_TAG_PREFIX.casefold()
    for raw in labels:
        value = str(raw or "").strip()
        if not value:
            continue
        if not value.casefold().startswith(prefix):
            continue
        suffix = value[len(_ADVENTURE_PLATE_SECTION_TAG_PREFIX):].strip()
        if suffix:
            out.add(suffix)
    return out


def _quest_source_path_token_tags(relative_path: str) -> set[str]:
    path = str(relative_path or "").replace("\\", "/").casefold().strip("/")
    if not path.startswith("duty/quest/"):
        return set()

    parts = [part for part in path.split("/") if part]
    raw_tokens: set[str] = set()

    for part in parts:
        stem = part[:-5] if part.endswith(".json") else part
        if not stem:
            continue
        raw_tokens.add(stem)
        for piece in re.split(r"[-_]+", stem):
            piece = piece.strip()
            if piece:
                raw_tokens.add(piece)

    tokens: set[str] = set()
    for token in raw_tokens:
        norm = _norm_lookup_key(token)
        if not norm or norm in _QUEST_PATH_STOP_TOKENS:
            continue
        tokens.add(norm)

    return {f"{_QUEST_PATH_TOKEN_TAG_PREFIX}{token}" for token in tokens}


def _quest_path_tokens_from_labels(labels: list[str] | tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    prefix = _QUEST_PATH_TOKEN_TAG_PREFIX.casefold()
    for raw in labels:
        value = str(raw or "").strip()
        if not value.casefold().startswith(prefix):
            continue
        token = _norm_lookup_key(value[len(_QUEST_PATH_TOKEN_TAG_PREFIX):])
        if token:
            out.add(token)
    return out


def _completion_payload_starting_class(payload: dict[str, Any]) -> str | None:
    value = payload.get("starting-class")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text


def _starting_city_for_class(starting_class: str | None) -> str | None:
    key = _norm_lookup_key(str(starting_class or ""))
    if not key:
        return None
    return _STARTING_CLASS_CITY_BY_KEY.get(key)


def _is_internal_source_label(label: str) -> bool:
    value = str(label or "").strip()
    if not value:
        return False
    value_cf = value.casefold()
    return any(value_cf.startswith(prefix.casefold()) for prefix in _INTERNAL_SOURCE_LABEL_PREFIXES)


def _candidate_match_labels(labels: list[str] | tuple[str, ...]) -> list[str]:
    filtered = [
        str(label).strip()
        for label in labels
        if isinstance(label, str) and str(label).strip() and not _is_internal_source_label(label)
    ]
    if filtered:
        return filtered
    return [str(label).strip() for label in labels if isinstance(label, str) and str(label).strip()]


def _hall_of_the_novice_role_key(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    key = _norm_lookup_key(text)
    if not key:
        return None
    if key in {"tank", "healer", "dps"}:
        return key
    if "dps" in key or key in {"melee", "ranged", "caster", "damage", "damagedealer"}:
        return "dps"
    return None


def _hall_of_the_novice_bucket_from_role(value: Any) -> str | None:
    role_key = _hall_of_the_novice_role_key(value)
    if not role_key:
        return None
    return f"duty/hall-of-the-novice/{role_key}"


def _row_buckets_for_sheet(
    sheet_name: str,
    section_label: str | None,
    row_json_obj: dict[str, Any] | None = None,
) -> set[str]:
    buckets = set(_sheet_buckets(sheet_name))
    sheet_norm = _norm_label(sheet_name)
    if sheet_norm == _norm_label("Guildhests"):
        class_bucket = _guildhests_bucket_from_section(section_label)
        if class_bucket:
            buckets.add(class_bucket)
    if sheet_norm == _norm_label("Hall of the Novice"):
        role_value: Any = section_label
        if isinstance(row_json_obj, dict):
            role_value = row_json_obj.get("class") or row_json_obj.get("role") or role_value
        role_bucket = _hall_of_the_novice_bucket_from_role(role_value)
        if role_bucket:
            buckets.add(role_bucket)
    if sheet_norm == _norm_label("Island Sanctuary - Crafting"):
        section_key = _norm_lookup_key(str(section_label or ""))
        if section_key in {"tools", "feed", "restraints"}:
            buckets.add(f"duty/island-sanctuary/crafting/{section_key}")
    if sheet_norm == _norm_label("Island Sanctuary - Isleventory"):
        section_key = _norm_lookup_key(str(section_label or ""))
        section_aliases = {
            "materials": "materials",
            "gardeningstarters": "gardening-starters",
            "produce": "produce",
        }
        suffix = section_aliases.get(section_key)
        if suffix:
            buckets.add(f"duty/island-sanctuary/isleventory/{suffix}")
    if sheet_norm == _norm_label("Collection"):
        buckets.add("duty/collection")
        section_key = _norm_lookup_key(str(section_label or ""))
        portable_aliases = {
            "portablearchive": "portable-archive",
            "thecopiedfactory": "the-copied-factory",
            "thepuppetsbunker": "the-puppets-bunker",
            "konoggsmessages": "konoggs-messages",
        }
        portable_suffix = portable_aliases.get(section_key)
        if portable_suffix:
            buckets.add("duty/collection/portable-archive")
            if portable_suffix != "portable-archive":
                buckets.add(f"duty/collection/portable-archive/{portable_suffix}")
    return buckets


def _aetheryte_type_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    norm = _norm_label(text)
    if "crystal" in norm:
        return "crystal"
    if "shard" in norm:
        return "shard"
    if text.startswith("@"):
        token = text.split(".")[-1]
        token_key = _norm_lookup_key(token)
        if token_key:
            return token_key
    return _norm_lookup_key(text)


def _aetheryte_zone_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.upper().startswith("@PLACE."):
        text = text[7:].replace("_", " ")
    return _norm_lookup_key(text)


def _aetheryte_signature(*, zone: Any, aeth_type: Any, name: Any) -> str | None:
    zone_key = _aetheryte_zone_key(zone)
    type_key = _aetheryte_type_key(aeth_type)
    name_key = _norm_lookup_key(str(name or ""))
    if not zone_key or not type_key or not name_key:
        return None
    return f"@AETHSIG.{zone_key}.{type_key}.{name_key}"


def _aetheryte_name_aliases(value: Any) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()

    aliases = {text}
    if text.casefold().startswith("the ") and len(text) > 4:
        aliases.add(text[4:].strip())

    # Desktop resources append location suffixes that workbook location_name
    # values often omit.
    for suffix in ("Aetheryte Plaza", "Shaded Bower"):
        if text.casefold().endswith(suffix.casefold()):
            base = text[: -len(suffix)].strip(" -")
            if base:
                aliases.add(base)

    return {alias for alias in aliases if alias}


def _aetheryte_signatures(*, zone: Any, aeth_type: Any, name: Any) -> set[str]:
    out: set[str] = set()
    for candidate_name in _aetheryte_name_aliases(name):
        signature = _aetheryte_signature(zone=zone, aeth_type=aeth_type, name=candidate_name)
        if signature:
            out.add(signature)
    return out


def _aetheryte_source_signatures(item: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    zone_value = item.get("zone")
    type_value = item.get("type")
    for field in ("name_en", "title_en", "name", "title"):
        value = item.get(field)
        if not isinstance(value, str):
            continue
        out.update(_aetheryte_signatures(zone=zone_value, aeth_type=type_value, name=value))
    return out


def _sheet_buckets(sheet_name: str) -> set[str]:
    name_norm = _norm_label(sheet_name)
    override = _SHEET_BUCKET_OVERRIDES.get(name_norm)
    if override:
        return set(override)

    out: set[str] = set()

    is_achievement_sheet = "achievement" in name_norm or "achiev" in name_norm
    # Achievement sheets should not also be indexed as generic quest sheets,
    # even when their title contains the word "quest".
    if any(token in name_norm for token in _QUEST_LIKE_SHEET_TOKENS_NORM) and not is_achievement_sheet:
        out.add("quest")
    if is_achievement_sheet:
        out.add("achievement")
    if "minion" in name_norm:
        out.add("minion")
    if "mount" in name_norm:
        out.add("mount")
    if "triple triad" in name_norm or "card" in name_norm:
        out.add("tripletriad")
    if "blue magic spellbook" in name_norm:
        out.add("bluemagic")
    if "emote" in name_norm:
        out.add("emote")
    if "orchestrion" in name_norm:
        out.add("orchestrion")
    return out


def _orchestrion_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}
    if value.casefold().endswith(" orchestrion roll"):
        aliases.add(value[:-17].strip())
    else:
        aliases.add(f"{value} Orchestrion Roll")
    return {a for a in aliases if a}


def _tripletriad_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}
    if value.casefold().endswith(" card"):
        base = value[:-5].strip()
        if base:
            aliases.add(base)
    else:
        aliases.add(f"{value} Card")

    # Some labels may include/omit a leading article.
    if value.casefold().startswith("the "):
        aliases.add(value[4:].strip())
    else:
        aliases.add(f"The {value}")

    # Lodestone and workbook may use '&' vs 'and' inconsistently.
    and_variants = set()
    for alias in aliases:
        and_variants.add(alias.replace("&", "and"))
        and_variants.add(re.sub(r"\band\b", "&", alias, flags=re.IGNORECASE))
    aliases.update(a.strip() for a in and_variants if a and a.strip())

    card_variants = set()
    for alias in aliases:
        if alias.casefold().endswith(" card"):
            card_variants.add(alias)
        else:
            card_variants.add(f"{alias} Card")
    aliases.update(card_variants)

    return aliases


_ROMAN_MAP = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}


def _suffix_roman_digit_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}

    m_roman = re.match(r"^(.*?)(?:\s+)(I|II|III|IV|V|VI|VII|VIII|IX|X)$", value)
    if m_roman:
        prefix = m_roman.group(1).strip()
        roman = m_roman.group(2)
        aliases.add(f"{prefix} {_ROMAN_MAP[roman]}")

    m_digit = re.match(r"^(.*?)(?:\s+)(\d{1,2})$", value)
    if m_digit:
        prefix = m_digit.group(1).strip()
        num = int(m_digit.group(2))
        for roman, mapped in _ROMAN_MAP.items():
            if mapped == num:
                aliases.add(f"{prefix} {roman}")
                break

    return aliases


def _qualifier_reorder_aliases(raw: str) -> set[str]:
    # Duty Finder sources name difficulty-qualified turns as
    # "<duty> (Savage) - Turn 1", while the workbook places the qualifier last
    # ("<duty> - Turn 1 (Savage)"). Generate both orderings so either layout
    # resolves to the same row.
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}

    # "<prefix> (<qual>) - <rest>" -> "<prefix> - <rest> (<qual>)"
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*-\s*(.+)$", value)
    if m:
        prefix, qual, rest = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if prefix and qual and rest:
            aliases.add(f"{prefix} - {rest} ({qual})")

    # "<prefix> - <rest> (<qual>)" -> "<prefix> (<qual>) - <rest>"
    m = re.match(r"^(.*?)\s*-\s*(.+?)\s*\(([^)]+)\)$", value)
    if m:
        prefix, rest, qual = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if prefix and rest and qual:
            aliases.add(f"{prefix} ({qual}) - {rest}")

    return aliases


def _achievement_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}

    # Lodestone and workbook may disagree on suffix style (I/II vs 1/2).
    m_roman = re.match(r"^(.*?)(?:\s+)(I|II|III|IV|V|VI|VII|VIII|IX|X)$", value)
    if m_roman:
        prefix = m_roman.group(1).strip()
        roman = m_roman.group(2)
        aliases.add(f"{prefix} {_ROMAN_MAP[roman]}")

    m_digit = re.match(r"^(.*?)(?:\s+)(\d{1,2})$", value)
    if m_digit:
        prefix = m_digit.group(1).strip()
        num = int(m_digit.group(2))
        for roman, mapped in _ROMAN_MAP.items():
            if mapped == num:
                aliases.add(f"{prefix} {roman}")
                break

    # Known Lodestone/workbook title variants.
    if "aetherfont" in value.casefold():
        aliases.add(re.sub(r"aetherfont", "Aetherfront", value, flags=re.IGNORECASE))

    if "good-bye" in value.casefold():
        aliases.add(re.sub(r"good-bye", "Goodbye", value, flags=re.IGNORECASE))

    if value.endswith("..."):
        aliases.add(value[:-3] + "…")

    return aliases


def _crafting_label_aliases(bucket: str, raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases = {value}
    bucket_norm = str(bucket or "").strip().casefold()
    rename_map = _CRAFTING_LABEL_RENAMES_BY_BUCKET_NORM.get(bucket_norm, {})
    renamed = rename_map.get(_norm_label(value))
    if renamed:
        aliases.add(renamed)

    return aliases


def _candidate_aliases(bucket: str, raw_label: str) -> list[str]:
    aliases: set[str]
    if bucket == "quest":
        aliases = _quest_label_aliases(raw_label)
    elif bucket == "tripletriad":
        aliases = _tripletriad_label_aliases(raw_label)
    elif bucket == "achievement":
        aliases = _achievement_label_aliases(raw_label)
    elif bucket == "orchestrion":
        aliases = _orchestrion_label_aliases(raw_label)
    elif bucket == "character/relic-gear/lucis-tools":
        value = raw_label.strip()
        aliases = {value}
        # Desktop source names the fisher Lucis chain as "Halcyon*" while the
        # workbook rows include "Halcyon Rod*".
        if value.casefold() == "halcyon":
            aliases.add("Halcyon Rod")
        elif value.casefold() == "halcyon supra":
            aliases.add("Halcyon Rod Supra")
        elif value.casefold() == "halcyon lucis":
            aliases.add("Halcyon Rod Lucis")
    elif bucket.startswith("duty/duty-raid-finder/"):
        aliases = _qualifier_reorder_aliases(raw_label)
    elif bucket.startswith("travel/porters/"):
        value = raw_label.strip()
        aliases = {value}
        # Desktop labels can include a leading article while workbook porter
        # locations usually omit it (e.g. "The Hawthorne Hut" -> "Hawthorne Hut").
        if value.casefold().startswith("the ") and len(value) > 4:
            aliases.add(value[4:].strip())
    elif bucket.startswith("logs/sightseeing-log/"):
        value = raw_label.strip()
        aliases = {value}
        if value.casefold().startswith("the ") and len(value) > 4:
            aliases.add(value[4:].strip())
    elif bucket.startswith("logs/crafting-log/"):
        aliases = _crafting_label_aliases(bucket, raw_label)
    else:
        aliases = {raw_label}

    expanded: set[str] = set()
    for alias in aliases:
        expanded.update(_suffix_roman_digit_aliases(alias))
    return sorted(a for a in expanded if a)


def _partial_match_hits(
    *,
    bucket: str,
    aliases: list[str],
    bucket_norm: dict[str, list[tuple[str, int, str]]],
    bucket_norm_keys: list[str],
) -> list[tuple[str, int, str]] | None:
    if not bucket_norm_keys:
        return None

    # Conservative by default; only rescue near-typo misses.
    cutoff = 0.95 if bucket == "quest" else 0.93
    if bucket == "tripletriad":
        cutoff = 0.85

    for alias in aliases:
        norm = _norm_label(alias)
        if len(norm) < 8:
            continue
        if bucket == "achievement" and re.search(r"\b(?:\d{1,2}|i|ii|iii|iv|v|vi|vii|viii|ix|x)$", norm):
            # Avoid cross-tier collisions like "... II" -> "... I".
            continue
        if bucket == "quest" and " " not in norm:
            # Single-token quest names can fuzzy-collide with job labels
            # (e.g. "Unbreaker" -> "Gunbreaker"). Keep quest fuzzy matching
            # to phrase-like labels where typo rescue is useful.
            continue
        close = difflib.get_close_matches(norm, bucket_norm_keys, n=1, cutoff=cutoff)
        if close:
            if bucket == "quest" and " " not in close[0]:
                continue
            hits = bucket_norm.get(close[0])
            if hits:
                return hits
    return None


def _index_labels_for_bucket(
    *,
    bucket: str,
    node_label: str,
    row_json_obj: dict[str, Any] | None,
) -> set[str]:
    labels = {node_label}
    if isinstance(row_json_obj, dict):
        field_by_bucket = {
            "quest": "quest",
            "bluemagic": "spell",
            "emote": "emote",
            "orchestrion": "orchestrion_roll",
        }
        field = field_by_bucket.get(bucket)
        if field is None and bucket.startswith("duty/duty-raid-finder/"):
            tail = _bucket_tail(bucket)
            if tail in {"dungeon", "raid", "guildhests", "deep-dungeons", "v-and-c-dungeons"}:
                field = "dungeon"
            elif tail == "trial":
                field = "trial"
        if field is None and bucket.startswith("duty/duty-raid-finder/guildhests/"):
            field = "dungeon"
        if field is None and bucket.startswith("duty/hall-of-the-novice/"):
            field = "quest"
        if isinstance(field, str):
            value = row_json_obj.get(field)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    labels.add(text)
                    if bucket.startswith("duty/duty-raid-finder/"):
                        labels.update(_qualifier_reorder_aliases(text))
                        m_duty = re.match(r"^(.*?)\s*\((?:duty)\)$", text, flags=re.IGNORECASE)
                        if m_duty:
                            base = m_duty.group(1).strip()
                            if base:
                                labels.add(base)
                    # Workbook sometimes combines two quest names with " / "
                    # (e.g. "Training with Leih / School of Hard Nocks").
                    # Index each part so either name from Lodestone matches.
                    if " / " in text:
                        for part in text.split(" / "):
                            part = part.strip()
                            if part:
                                labels.add(part)

        if bucket.startswith("duty/island-sanctuary/rank"):
            rank_raw = row_json_obj.get("sanctuary_rank")
            rank_num: int | None = None
            if isinstance(rank_raw, int) and not isinstance(rank_raw, bool):
                rank_num = rank_raw
            elif isinstance(rank_raw, float) and rank_raw.is_integer():
                rank_num = int(rank_raw)
            elif isinstance(rank_raw, str):
                text = rank_raw.strip()
                if text.isdigit():
                    rank_num = int(text)

            if isinstance(rank_num, int) and rank_num > 0:
                labels.add(str(rank_num))
                labels.update(_suffix_roman_digit_aliases(f"Rank {rank_num}"))
                labels.update(_suffix_roman_digit_aliases(f"Sanctuary Rank {rank_num}"))

        if bucket == "travel/aetherytes" or bucket.startswith("travel/aetherytes/"):
            zone_name = row_json_obj.get("zone_name")
            type_name = row_json_obj.get("type")
            location_name = row_json_obj.get("location_name")
            labels.update(
                _aetheryte_signatures(
                    zone=zone_name,
                    aeth_type=type_name,
                    name=location_name,
                )
            )
            if isinstance(location_name, str):
                text = location_name.strip()
                if text:
                    labels.add(text)
    return labels


def _unmatched_reason(
    *,
    bucket: str,
    raw_label: str,
    aliases: list[str],
    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]],
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]],
) -> tuple[str, dict[str, Any]]:
    alias_norms = [_norm_label(a) for a in aliases if _norm_label(a)]

    other_buckets: set[str] = set()
    for alias in aliases:
        key = alias.casefold()
        for b, b_exact in exact_idx.items():
            if b == bucket:
                continue
            if key in b_exact:
                other_buckets.add(b)
    for alias_norm in alias_norms:
        for b, b_norm in norm_idx.items():
            if b == bucket:
                continue
            if alias_norm in b_norm:
                other_buckets.add(b)

    if other_buckets:
        return "mapped_to_other_bucket", {
            "other_buckets": sorted(other_buckets),
        }

    if bucket == "tripletriad":
        triad_norm = norm_idx.get("tripletriad", {})
        for alias in aliases:
            stripped = alias.strip()
            if not stripped:
                continue
            if stripped.casefold().endswith(" card"):
                alt = stripped[:-5].strip()
            else:
                alt = f"{stripped} Card"
            alt_norm = _norm_label(alt)
            if alt_norm and alt_norm in triad_norm:
                return "label_variant_card_suffix", {"suggested_alias": alt}

    if bucket == "quest" and re.match(r"^.+?\s*\(.+\)\s*$", raw_label.strip()):
        return "quest_wrapper_unresolved", {}

    return "not_found_in_workbook", {}


def _empty_sidecar(character: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": progress_io.SCHEMA_VERSION,
        "character": {
            "name": character["name"],
            "starting_class": character["starting_class"],
            "created_at": character["created_at"] or dt.datetime.now().isoformat(timespec="seconds"),
        },
        "progress": [],
    }


def reset_character_progress(
    conn: sqlite3.Connection,
    character: sqlite3.Row,
    run_id: int,
) -> None:
    conn.execute(
        "DELETE FROM character_progress WHERE character_id = ? AND run_id = ?",
        (character["id"], run_id),
    )
    conn.execute(
        "DELETE FROM progress_rollup WHERE character_id = ? AND run_id = ?",
        (character["id"], run_id),
    )
    conn.commit()

    sidecar = progress_io.sidecar_path(character["name"])
    progress_io.save_sidecar(sidecar, _empty_sidecar(character))
    progress_io.invalidate_cache(sidecar)


def _collect_lodestone_class_job_levels(payload: dict[str, Any]) -> dict[str, float]:
    """Return normalized Classes-Jobs label -> level values from Lodestone payload."""
    out: dict[str, float] = {}
    class_job = payload.get("class_job")
    if not isinstance(class_job, dict):
        return out

    for rows in class_job.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            job_name = str(row.get("job") or "").strip()
            if not job_name:
                continue
            level_raw = row.get("level")
            if not isinstance(level_raw, (int, float)):
                continue
            level = max(0.0, float(level_raw))
            key = _norm_label(job_name)
            if not key:
                continue
            prev = out.get(key)
            if prev is None or level > prev:
                out[key] = level
    return out


def _numeric_or_none(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.replace(",", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _apply_lodestone_class_job_levels(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    run_id: int,
    starting_class: str | None,
    levels_by_label: dict[str, float],
    merge_mode: str,
) -> tuple[int, int]:
    """Apply Lodestone class/job levels onto Classes-Jobs value rows.

    Returns (rows_applied, rows_skipped).
    """
    if not levels_by_label:
        return 0, 0

    rows = conn.execute(
        """
        SELECT n.row_index, n.label, n.row_json,
               s.value_key,
               p.progress_percent
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        LEFT JOIN character_progress p
          ON p.character_id = ? AND p.run_id = n.run_id
         AND p.sheet_name = n.sheet_name AND p.row_index = n.row_index
        WHERE n.run_id = ?
          AND n.sheet_name = 'Classes-Jobs'
          AND n.row_type = 'value'
          AND n.label IS NOT NULL
        ORDER BY n.row_index
        """,
        (character_id, run_id),
    ).fetchall()

    applied = 0
    skipped = 0

    for row in rows:
        label = str(row["label"] or "").strip()
        if not label:
            continue
        key = _norm_label(label)
        if not key:
            continue
        incoming = levels_by_label.get(key)
        if incoming is None:
            continue

        existing = _numeric_or_none(row["progress_percent"])
        if existing is None:
            row_json_obj: dict[str, Any] | None = None
            row_json_raw = row["row_json"]
            if isinstance(row_json_raw, str) and row_json_raw.strip():
                try:
                    decoded = json.loads(row_json_raw)
                    if isinstance(decoded, dict):
                        row_json_obj = decoded
                except json.JSONDecodeError:
                    row_json_obj = None
            value_key = str(row["value_key"] or "")
            if row_json_obj is not None and value_key:
                existing = _numeric_or_none(row_json_obj.get(value_key))
        if existing is None:
            existing = 0.0

        target = incoming if merge_mode == "overwrite" else max(existing, incoming)
        if abs(target - existing) < 1e-9:
            skipped += 1
            continue

        db.set_row_value(
            conn,
            character_id,
            run_id,
            "Classes-Jobs",
            int(row["row_index"]),
            target,
            commit=False,
            starting_class=starting_class,
        )
        applied += 1

    return applied, skipped


def import_lodestone_payload(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    payload_path: Path,
    clear_existing: bool = False,
    level_merge_mode: str = "keep-highest",
    progress: Callable[[str], None] | None = None,
) -> ImportSummary:
    def log(message: str) -> None:
        if progress is not None:
            progress(message)

    run_id = db.latest_run_id(conn)
    if run_id is None:
        raise ValueError("No ingest run found. Run scripts/prep_xlsx_to_sqlite.py first.")

    character = db.get_character(conn, character_id)
    if character is None:
        raise ValueError(f"Character id {character_id} was not found")

    log(f"Loading payload: {payload_path}")
    payload = load_payload(payload_path)
    _save_avatar(payload, character_id, log)
    level_mode = level_merge_mode if level_merge_mode in LODESTONE_LEVEL_MERGE_MODES else "keep-highest"
    class_job_levels = _collect_lodestone_class_job_levels(payload)
    if class_job_levels:
        log(
            "Collected Lodestone class/job levels: "
            f"{len(class_job_levels)} rows (merge mode: {level_mode})"
        )
    else:
        log("No class/job levels found in Lodestone payload")
    candidates = collect_candidates(payload)
    total_candidates = sum(len(v) for v in candidates.values())
    log(
        "Collected candidates: "
        + ", ".join(f"{bucket}={len(values)}" for bucket, values in candidates.items())
    )

    if clear_existing:
        log("Clearing existing character progress before import")
        reset_character_progress(conn, character, run_id)

    rows = conn.execute(
        """
            SELECT n.sheet_name, n.row_index, n.section_label, n.label, n.row_type, n.row_json
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        WHERE n.run_id = ?
          AND s.is_menu = 0
          AND n.label IS NOT NULL
          AND n.row_type IN ('checkbox', 'value')
        """,
        (run_id,),
    ).fetchall()

    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}

    for row in rows:
        sheet_name = row["sheet_name"]
        section_label = str(row["section_label"] or "")
        label = (row["label"] or "").strip()
        if not label:
            continue
        entry = (sheet_name, int(row["row_index"]), row["row_type"])
        row_json_obj: dict[str, Any] | None = None
        row_json_text = row["row_json"]
        if isinstance(row_json_text, str) and row_json_text.strip():
            try:
                decoded = json.loads(row_json_text)
                if isinstance(decoded, dict):
                    row_json_obj = decoded
            except json.JSONDecodeError:
                row_json_obj = None
        for bucket in _row_buckets_for_sheet(
            sheet_name,
            section_label,
            row_json_obj=row_json_obj,
        ):
            for idx_label in _index_labels_for_bucket(
                bucket=bucket,
                node_label=label,
                row_json_obj=row_json_obj,
            ):
                exact_idx.setdefault(bucket, {}).setdefault(idx_label.casefold(), []).append(entry)
                norm = _norm_label(idx_label)
                if norm:
                    norm_idx.setdefault(bucket, {}).setdefault(norm, []).append(entry)

    targets: dict[tuple[str, int], str] = {}
    norm_keys_idx: dict[str, list[str]] = {
        bucket: list(bucket_norm.keys()) for bucket, bucket_norm in norm_idx.items()
    }
    matched_candidates = 0
    unmatched_candidates = 0
    ignored_untracked_emotes = 0
    unmatched_items: list[dict[str, Any]] = []

    for bucket, labels in candidates.items():
        bucket_exact = exact_idx.get(bucket, {})
        bucket_norm = norm_idx.get(bucket, {})
        bucket_norm_keys = norm_keys_idx.get(bucket, [])
        for raw_label in sorted(labels):
            hits: list[tuple[str, int, str]] | None = None
            aliases = _candidate_aliases(bucket, raw_label)
            for alias in aliases:
                hits = bucket_exact.get(alias.casefold())
                if hits:
                    break
                hits = bucket_norm.get(_norm_label(alias))
                if hits:
                    break
            if not hits:
                hits = _partial_match_hits(
                    bucket=bucket,
                    aliases=aliases,
                    bucket_norm=bucket_norm,
                    bucket_norm_keys=bucket_norm_keys,
                )
            if not hits:
                reason, reason_extra = _unmatched_reason(
                    bucket=bucket,
                    raw_label=raw_label,
                    aliases=aliases,
                    exact_idx=exact_idx,
                    norm_idx=norm_idx,
                )

                # Lodestone includes many built-in/default emotes that are not
                # represented in the workbook. Keep reports focused on truly
                # actionable misses by skipping those untracked emote entries.
                if bucket == "emote" and reason in {"not_found_in_workbook", "mapped_to_other_bucket"}:
                    ignored_untracked_emotes += 1
                    continue

                unmatched_candidates += 1
                unmatched_item = {
                    "bucket": bucket,
                    "label": raw_label,
                    "reason": reason,
                    "attempted_aliases": aliases[:6],
                }
                unmatched_item.update(reason_extra)
                unmatched_items.append(unmatched_item)
                continue
            matched_candidates += 1
            for sheet_name, row_index, row_type in hits:
                targets[(sheet_name, row_index)] = row_type

    log(
        f"Matched {matched_candidates}/{total_candidates} candidates; "
        f"resolved to {len(targets)} workbook rows"
    )

    rows_applied = 0
    rows_skipped = 0
    level_rows_applied = 0
    level_rows_skipped = 0
    starting_class = character["starting_class"]

    with progress_io.batch(conn, character_id):
        ordered_targets = sorted(targets.items(), key=lambda item: (item[0][0], item[0][1]))
        for idx, ((sheet_name, row_index), row_type) in enumerate(ordered_targets, start=1):
            current = db.effective_state(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
                starting_class,
            )
            if current == "done":
                rows_skipped += 1
                continue

            if row_type == "value":
                db.set_row_value(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    100.0,
                    commit=False,
                    starting_class=starting_class,
                )
            else:
                db.set_row_state(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    "done",
                    commit=False,
                    starting_class=starting_class,
                )
            rows_applied += 1

            if idx % 50 == 0:
                log(f"Applied {idx}/{len(ordered_targets)} matched rows")

        level_rows_applied, level_rows_skipped = _apply_lodestone_class_job_levels(
            conn,
            character_id=character_id,
            run_id=run_id,
            starting_class=starting_class,
            levels_by_label=class_job_levels,
            merge_mode=level_mode,
        )

    rows_applied += level_rows_applied
    rows_skipped += level_rows_skipped

    conn.commit()

    log(
        f"Import complete: applied={rows_applied}, already_done={rows_skipped}, "
        f"unmatched_candidates={unmatched_candidates}"
    )
    if ignored_untracked_emotes:
        log(f"Ignored untracked emote candidates: {ignored_untracked_emotes}")
    if unmatched_items:
        reason_counts = Counter(str(item.get("reason") or "unknown") for item in unmatched_items)
        reason_msg = ", ".join(
            f"{reason}={count}" for reason, count in sorted(reason_counts.items())
        )
        log(f"Unmatched reasons: {reason_msg}")
        sample = ", ".join(
            f"{item['bucket']}:{item['label']} ({item.get('reason')})"
            for item in unmatched_items[:6]
        )
        log(f"Unmatched sample: {sample}")
    if class_job_levels:
        log(
            "Classes-Jobs level sync: "
            f"applied={level_rows_applied}, skipped={level_rows_skipped}"
        )

    return ImportSummary(
        character_id=character_id,
        character_name=character["name"],
        source_path=str(payload_path),
        run_id=run_id,
        total_candidates=total_candidates,
        matched_candidates=matched_candidates,
        unmatched_candidates=unmatched_candidates,
        rows_applied=rows_applied,
        rows_skipped_already_done=rows_skipped,
        unmatched_items=unmatched_items,
    )


def format_exception(exc: Exception) -> str:
    return "\n".join(traceback.format_exception(exc))

# --- Desktop completion import ---------------------------------------------

# Canonical buckets with established alias/matching behavior. Any other
# completion path group still imports via dynamic fallback buckets.
CANONICAL_BUCKETS = (
    "quest",
    "achievement",
    "minion",
    "mount",
    "tripletriad",
    "bluemagic",
    "emote",
    "orchestrion",
)

_SEGMENT_ALIASES = {
    "hunting-log": "hunting",
    "classes--jobs": "classes-jobs",
    "order-of-the-twin-adder": "twin-adder",
    "resplendent": "resplendent-tools",
    "dungeons": "dungeon",
    "trials": "trial",
    "raids": "raid",
}

_CANONICAL_SOURCE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "minion": ("character/adventure-plate/minion",),
    "mount": ("character/adventure-plate/mount",),
}

_PLACE_ZONE_NORM_ALIASES = {
    "raktika": "theraktikagreatwood",
}

_IGNORED_UNTRACKED_BUCKETS = {
    "character/gold-saucer/chocobo",
    "duty/squadron",
    "duty/squadron/stats",
}

# Known desktop source entries with no workbook row mapping in this repo's
# tracker data should be skipped when unmatched so reports stay actionable.
_IGNORED_UNTRACKED_SOURCE_IDS_BY_BUCKET: dict[str, frozenset[str]] = {
    # Desktop source includes non-rare island fauna while workbook only tracks
    # the rare-animal checklist entries.
    "duty/island-sanctuary/animals": frozenset({
        "0",  # Lost Lamb
        "2",  # Opo-Opo
        "4",  # Apkallu
        "6",  # Ground Squirrel
        "8",  # Coblyn
        "12",  # Wild Dodo
        "14",  # Island Doe
        "16",  # Chocobo
        "18",  # Glyptodon Pup
        "21",  # Aurochs
        "23",  # Island Nanny
        "25",  # Blue Back
    }),
    # Automation upgrades are source-only entries; workbook tracks building
    # completion by unlocked plot rows, not separate automation rows.
    "duty/island-sanctuary/buildings": frozenset({"6", "10"}),
    # These two material entries are present in desktop resources but not in
    # the current workbook material checklist.
    "duty/island-sanctuary/isleventory/materials": frozenset({"27", "28"}),
    # Desktop collection index has a root marker entry, not a checklist row.
    "duty/collection": frozenset({"0"}),
    # These source rows currently have no workbook counterpart in their
    # destination crafting logs.
    "logs/crafting-log/armorer": frozenset({"6202"}),
    "logs/crafting-log/leatherworker": frozenset({"360"}),
}

# Buckets with no reliable workbook destination should be skipped entirely to
# avoid cross-sheet label collisions from broad fallback matching.
_QUARANTINED_BUCKET_PREFIXES = (
    # Squadron entries from desktop are not represented as workbook rows.
    "duty/squadron/",
    # Treasure-hunt maps/duties are source-side progress markers only.
    "duty/treasure-hunt/maps",
    "duty/treasure-hunt/duties",
    # No dedicated deep-dungeons tracker sheet exists in the workbook.
    "duty/duty-raid-finder/deep-dungeons",
    "duty/squadron/command-missions",
    "duty/trust/",
)


def _is_quarantined_bucket(bucket: str) -> bool:
    value = str(bucket or "").strip().casefold()
    if not value:
        return False
    return any(value.startswith(prefix) for prefix in _QUARANTINED_BUCKET_PREFIXES)

_POSITIONAL_VALUE_BUCKETS = {
    "classes-jobs",
    "desynthesis",
}

_REFERENCE_ID_RE = re.compile(r"^([A-Za-z])\.(\d+)$")

_REFERENCE_BUCKET_BY_PREFIX = {
    "q": "quest",
    "a": "achievement",
}


def _path_group_key(parts: list[str]) -> str | None:
    segments: list[str] = []
    for raw_part in parts:
        cleaned = str(raw_part).strip().casefold()
        if not cleaned:
            continue
        cleaned = re.sub(r"-{2,}", "-", cleaned)
        cleaned = _SEGMENT_ALIASES.get(cleaned, cleaned)
        segments.append(cleaned)
    if not segments:
        return None

    if segments[0] == "overall":
        segments = segments[1:]
    if not segments:
        return None

    if re.fullmatch(r"[xX]?\d+", segments[-1]):
        segments = segments[:-1]
    if not segments:
        return None

    if segments[-1].endswith(".json"):
        stem = segments[-1][:-5]
        if stem in {"", "_index", "index"}:
            segments = segments[:-1]
        else:
            segments[-1] = stem
    if not segments:
        return None

    if segments[:3] == ["character", "blue-mage", "log"] and len(segments) >= 4:
        return "/".join(segments[:4])

    # Keep subgroup segment for categories that are partitioned by a meaningful
    # fourth component (sub-log or job path) so ids remain scoped.
    keep_four_prefixes = {
        ("duty", "exploratory-missions", "bozja"),
        ("logs", "gathering", "gathering-log"),
        ("duty", "duty-raid-finder", "guildhests"),
        ("duty", "island-sanctuary", "crafting"),
        ("duty", "island-sanctuary", "isleventory"),
        ("duty", "collection", "portable-archive"),
    }
    if tuple(segments[:3]) in keep_four_prefixes and len(segments) >= 4:
        return "/".join(segments[:4])

    key = "/".join(segments[:3])
    return key or None


def _normalize_numeric_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        m = re.fullmatch(r"[xX]?(\d+)", raw)
        if m:
            return str(int(m.group(1)))
        return None
    return None


def _is_ignored_untracked_candidate(bucket: str, source_id: Any) -> bool:
    bucket_key = str(bucket or "").strip().casefold()
    if not bucket_key:
        return False
    if bucket_key in _IGNORED_UNTRACKED_BUCKETS:
        return True

    ignored_ids = _IGNORED_UNTRACKED_SOURCE_IDS_BY_BUCKET.get(bucket_key)
    if not ignored_ids:
        return False

    source_id_key = _normalize_numeric_id(source_id)
    return bool(source_id_key and source_id_key in ignored_ids)


def default_completion_path() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata).expanduser() / "ffxiv-completionist" / "completion.json"


def list_detected_completion_files(limit: int = 20) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    default_path = default_completion_path()
    if default_path and default_path.exists() and default_path.is_file():
        out.append(default_path)
        seen.add(str(default_path).casefold())

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return out

    base = Path(appdata).expanduser() / "ffxiv-completionist"
    if not base.exists() or not base.is_dir():
        return out

    extra_patterns = (
        "completion*.json",
        "*completion*.json",
        "*.completion.json",
    )
    for pattern in extra_patterns:
        for candidate in sorted(base.glob(pattern), key=lambda p: p.name.lower()):
            if not candidate.exists() or not candidate.is_file():
                continue
            key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= limit:
                return out

    return out


def _resource_root_candidates() -> list[Path]:
    localapp = os.environ.get("LOCALAPPDATA")
    if not localapp:
        return []

    local_base = Path(localapp).expanduser()
    return [
        local_base / "Programs" / "ffxiv-completionist" / "resources" / "resources",
    ]


def resolve_resource_root() -> Path | None:
    for path in _resource_root_candidates():
        if path.exists() and path.is_dir():
            return path
    return None


def _resource_bucket_for_path(relative_path: str) -> str | None:
    path = "/" + relative_path.replace("\\", "/").casefold().strip("/")
    if "/logs/orchestrion-list/" in path:
        return "orchestrion"
    if "/social/emotes/" in path or "/social/emote/" in path:
        return "emote"
    if "/character/blue-mage/spellbook" in path:
        return "bluemagic"
    if "/gold-saucer/triple-triad-card-list/" in path:
        return "tripletriad"
    if "/character/achievement/" in path:
        return "achievement"
    if "/duty/quest/" in path:
        return "quest"
    if "/character/mount/" in path or path.endswith("/character/mount-guide.json"):
        return "mount"
    if "/character/minion/" in path or path.endswith("/character/minion-guide.json"):
        return "minion"
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    return _path_group_key(parts)


def _extract_labels(item: dict[str, Any]) -> list[str]:
    labels: list[str] = []

    def add_text(raw: str) -> None:
        text = raw.strip()
        if text and text not in labels and len(text) <= 180:
            labels.append(text)

    # Match ONLY on an item's canonical name. Every resolvable resource item
    # carries one of these primary fields, so this loses no recall — while
    # dropping secondary descriptive fields (description_en, npc_en, unlock_en,
    # mob_en, zone_en, source_en, ...). Those secondary fields are generic
    # prose ("Return", "Mother Miounne", "Successfully complete 5 dungeons")
    # that previously leaked into the alias pool and caused one source id to
    # collide with dozens of unrelated workbook rows in the global fallback.
    # Note: shared-fate / aether-current tasks store their @PLACE / @TRAVEL
    # tokens in the primary ``name`` field, so their special-case handlers are
    # unaffected.
    primary_fields = ("name_en", "title_en", "name", "title")
    for field in primary_fields:
        value = item.get(field)
        if isinstance(value, str):
            add_text(value)

    return labels


def _extract_reference_ids(value: Any) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            m = _REFERENCE_ID_RE.fullmatch(node.strip())
            if m:
                refs.add((m.group(1).lower(), str(int(m.group(2)))))
            return
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if isinstance(node, dict):
            for child in node.values():
                visit(child)

    visit(value)
    return refs


@lru_cache(maxsize=4)
def _build_source_label_index(resource_root_str: str) -> dict[str, dict[str, tuple[str, ...]]]:
    resource_root = Path(resource_root_str)
    index: dict[str, dict[str, set[str]]] = {}
    refs_by_source: dict[tuple[str, str], set[tuple[str, str]]] = {}

    for json_path in resource_root.rglob("*.json"):
        try:
            relative = json_path.relative_to(resource_root).as_posix()
        except ValueError:
            continue
        bucket = _resource_bucket_for_path(relative)
        if bucket is None:
            continue

        try:
            doc = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue

        bucket_index = index.setdefault(bucket, {})

        tasks = doc.get("tasks")
        if isinstance(tasks, dict):
            for task_key, item in tasks.items():
                if not isinstance(item, dict):
                    continue
                id_key = _normalize_numeric_id(task_key)
                if id_key is None:
                    id_key = _normalize_numeric_id(item.get("id"))
                if id_key is None:
                    continue
                labels = _extract_labels(item)
                refs = _extract_reference_ids(item)
                if not labels and not refs:
                    continue
                label_set = bucket_index.setdefault(id_key, set())
                label_set.update(labels)
                if bucket.startswith("character/adventure-plate"):
                    label_set.update(_adventure_plate_source_section_tags(item))
                if bucket == "quest":
                    label_set.update(_quest_source_path_token_tags(relative))
                if bucket.startswith("travel/aetherytes/"):
                    label_set.update(_aetheryte_source_signatures(item))
                if refs:
                    refs_by_source.setdefault((bucket, id_key), set()).update(refs)
            continue

        data = doc.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                id_key = _normalize_numeric_id(item.get("id"))
                if id_key is None:
                    continue
                labels = _extract_labels(item)
                refs = _extract_reference_ids(item)
                if not labels and not refs:
                    continue
                label_set = bucket_index.setdefault(id_key, set())
                label_set.update(labels)
                if bucket.startswith("character/adventure-plate"):
                    label_set.update(_adventure_plate_source_section_tags(item))
                if bucket == "quest":
                    label_set.update(_quest_source_path_token_tags(relative))
                if bucket.startswith("travel/aetherytes/"):
                    label_set.update(_aetheryte_source_signatures(item))
                if refs:
                    refs_by_source.setdefault((bucket, id_key), set()).update(refs)

    # Resolve symbolic links (q.<id>, a.<id>, etc.) into concrete names so
    # buckets with tokenized labels still have workbook-matchable aliases.
    for (source_bucket, source_id), refs in refs_by_source.items():
        source_labels = index.get(source_bucket, {}).get(source_id)
        if source_labels is None:
            continue
        # Only resolve symbolic references for items that have no real name of
        # their own -- i.e. their label is a token like "@PLACE.X". Items that
        # already carry a human-readable name (achievements, quests, ranks) must
        # NOT absorb their chain neighbours' names: cNext/cPrev links would
        # otherwise merge a whole achievement series ("To the Dungeons I..VI")
        # into one label set whose first sorted alias ("...I") then steals every
        # tier's match, leaving II-VI unmatched.
        if not all(str(label).startswith("@") for label in source_labels):
            continue
        for prefix, ref_id in refs:
            target_bucket = _REFERENCE_BUCKET_BY_PREFIX.get(prefix)
            if not target_bucket:
                continue
            target_labels = index.get(target_bucket, {}).get(ref_id)
            if target_labels:
                source_labels.update(target_labels)

    frozen: dict[str, dict[str, tuple[str, ...]]] = {}
    for bucket, mapping in index.items():
        frozen[bucket] = {
            id_key: tuple(sorted(labels))
            for id_key, labels in mapping.items()
            if labels
        }
    return frozen


def _bucket_lookup_chain(bucket: str) -> list[str]:
    value = bucket.strip().strip("/")
    if not value:
        return []
    out = [value]
    while "/" in value:
        value = value.rsplit("/", 1)[0]
        out.append(value)
    return out


def _lookup_source_labels(
    source_index: dict[str, dict[str, tuple[str, ...]]],
    *,
    bucket: str,
    source_id: str,
) -> tuple[tuple[str, ...] | None, str | None]:
    for candidate_bucket in _bucket_lookup_chain(bucket):
        labels = source_index.get(candidate_bucket, {}).get(source_id)
        if labels:
            return labels, candidate_bucket

    for candidate_bucket in _CANONICAL_SOURCE_FALLBACKS.get(bucket, ()):
        labels = source_index.get(candidate_bucket, {}).get(source_id)
        if labels:
            return labels, candidate_bucket

    global_hits: list[tuple[str, tuple[str, ...]]] = []
    for candidate_bucket, bucket_values in source_index.items():
        labels = bucket_values.get(source_id)
        if labels:
            global_hits.append((candidate_bucket, labels))

    if len(global_hits) == 1:
        return global_hits[0][1], global_hits[0][0]
    return None, None


def _build_inline_completion_source_index(
    payload: dict[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Build id->label lookup from metadata embedded in completion.json.

    Desktop payloads can include custom-entry metadata under top-level
    ``custom`` (e.g. ``{"x804": {"name": "Turali Alumen"}}``). Those ids
    are present in ``overall.custom`` progress leaves, but are not represented
    in bundled resource JSON files. Without this fallback they become
    ``id_not_in_source_index`` and never reach workbook matching.
    """
    index: dict[str, dict[str, set[str]]] = {}

    custom = payload.get("custom")
    if isinstance(custom, dict):
        custom_idx = index.setdefault("custom", {})
        for raw_id, raw_item in custom.items():
            id_key = _normalize_numeric_id(raw_id)
            if id_key is None:
                continue

            labels: set[str] = set()
            if isinstance(raw_item, dict):
                labels.update(_extract_labels(raw_item))

                # Some desktop custom entries may use ad-hoc label fields.
                for field in ("label", "display_name"):
                    value = raw_item.get(field)
                    if isinstance(value, str):
                        text = value.strip()
                        if text:
                            labels.add(text)
            elif isinstance(raw_item, str):
                text = raw_item.strip()
                if text:
                    labels.add(text)

            if not labels:
                continue

            custom_idx.setdefault(id_key, set()).update(labels)

    frozen: dict[str, dict[str, tuple[str, ...]]] = {}
    for bucket, mapping in index.items():
        frozen[bucket] = {
            id_key: tuple(sorted(labels))
            for id_key, labels in mapping.items()
            if labels
        }
    return frozen


def _merge_source_indexes(
    base: dict[str, dict[str, tuple[str, ...]]],
    extra: dict[str, dict[str, tuple[str, ...]]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    if not extra:
        return base

    merged_sets: dict[str, dict[str, set[str]]] = {}

    for bucket, mapping in base.items():
        bucket_map = merged_sets.setdefault(bucket, {})
        for source_id, labels in mapping.items():
            bucket_map.setdefault(source_id, set()).update(
                label for label in labels if isinstance(label, str) and label.strip()
            )

    for bucket, mapping in extra.items():
        bucket_map = merged_sets.setdefault(bucket, {})
        for source_id, labels in mapping.items():
            bucket_map.setdefault(source_id, set()).update(
                label for label in labels if isinstance(label, str) and label.strip()
            )

    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for bucket, mapping in merged_sets.items():
        out[bucket] = {
            source_id: tuple(sorted(labels))
            for source_id, labels in mapping.items()
            if labels
        }
    return out


def _completion_bucket_from_path(path_parts: tuple[str, ...]) -> str | None:
    joined = "/" + "/".join(str(part).casefold() for part in path_parts if part)
    if "/logs/orchestrion-list/" in joined:
        return "orchestrion"
    if "/social/emotes/" in joined or "/social/emote/" in joined:
        return "emote"
    if "/character/blue-mage/spellbook/" in joined:
        return "bluemagic"
    if "triple-triad-card-list" in joined:
        return "tripletriad"
    if "/character/achievement/" in joined:
        return "achievement"
    if "/duty/quest/" in joined:
        return "quest"
    if "/mount-guide/" in joined or "/character/mount/" in joined:
        return "mount"
    # Adventure Plate minion paths are semantically separate from the Minions
    # log and must keep their scoped bucket so hit filtering can pin them to
    # the Adventurer Plate sheet.
    if "/minion-guide/" in joined or "/character/minion/" in joined:
        return "minion"
    return _path_group_key([str(part) for part in path_parts])


def _norm_lookup_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _norm_label(value))


def _bucket_tail(bucket: str) -> str:
    value = str(bucket or "").strip()
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


# Some desktop buckets are semantically tied to a specific workbook sheet and
# should never resolve through the broad section-global fallback.
_BUCKET_ALLOWED_SHEETS_PREFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    ("logs/crafting-log/alchemist", frozenset({"Alchemy Log"})),
    ("logs/crafting-log/armorer", frozenset({"Armorcrafting Log"})),
    ("logs/crafting-log/blacksmith", frozenset({"Blacksmithing Log"})),
    ("logs/crafting-log/carpenter", frozenset({"Carpentry Log"})),
    ("logs/crafting-log/culinarian", frozenset({"Culinary Log"})),
    ("logs/crafting-log/goldsmith", frozenset({"Goldsmithing Log"})),
    ("logs/crafting-log/leatherworker", frozenset({"Leatherworking Log"})),
    ("logs/crafting-log/shared", frozenset({"Shared Craft Log"})),
    ("logs/crafting-log/weaver", frozenset({"Weaving Log"})),
    ("character/gold-saucer/triple-triad-opponents", frozenset({"Triple Triad Opponents"})),
    ("duty/duty-raid-finder/dungeon", frozenset({"Dungeons"})),
    ("duty/duty-raid-finder/trial", frozenset({"Trials"})),
    ("duty/duty-raid-finder/raid", frozenset({"Raids"})),
    ("duty/duty-raid-finder/guildhests", frozenset({"Guildhests"})),
    ("duty/duty-raid-finder/v-and-c-dungeons", frozenset({"V&C Dungeon Finder"})),
    ("duty/hall-of-the-novice", frozenset({"Hall of the Novice"})),
    (
        "duty/island-sanctuary/animals",
        frozenset({"Island Sanctuary - Rare Animals", "Island Sanctuary - Animals"}),
    ),
    ("duty/island-sanctuary/buildings", frozenset({"Island Sanctuary - Buildings"})),
    ("duty/island-sanctuary/crafting", frozenset({"Island Sanctuary - Crafting"})),
    ("duty/island-sanctuary/isleventory", frozenset({"Island Sanctuary - Isleventory"})),
    ("duty/island-sanctuary/rank", frozenset({"Island Sanctuary - Rank"})),
    (
        "duty/exploratory-missions/bozja/aetherytes",
        frozenset({"Bozja - Aetherytes"}),
    ),
    (
        "duty/exploratory-missions/bozja/duties",
        frozenset({"Bozja - Duties"}),
    ),
    (
        "duty/exploratory-missions/bozja/events",
        frozenset({"Bozja - Events"}),
    ),
    (
        "duty/exploratory-missions/bozja/lost-actions",
        frozenset({"Bozja - Lost Actions"}),
    ),
    (
        "duty/exploratory-missions/bozja/resistance-rank",
        frozenset({"Bozja - Resistance Rank"}),
    ),
    (
        "duty/exploratory-missions/bozja/resistance-honors",
        frozenset({"Bozja - Resistance Rank"}),
    ),
    (
        "logs/gathering/gathering-log/mining",
        frozenset({"Miner Logs"}),
    ),
    (
        "logs/gathering/gathering-log/quarrying",
        frozenset({"Miner Logs"}),
    ),
    (
        "logs/gathering/gathering-log/logging",
        frozenset({"Botanist Logs"}),
    ),
    (
        "logs/gathering/gathering-log/harvesting",
        frozenset({"Botanist Logs"}),
    ),
    ("logs/sightseeing-log/", frozenset({"Sightseeing Logs"})),
    ("logs/gathering/fishing-log", frozenset({"Fishing Logs"})),
    ("logs/gathering/fishing-guide", frozenset({"Fish Guide"})),
    ("travel/mount-speed/", frozenset({"Mount Speed"})),
    ("travel/porters/", frozenset({"Porters"})),
    ("travel/aetherytes/", frozenset({"Aetherytes"})),
    ("duty/collection", frozenset({"Collection"})),
    ("character/adventure-plate", frozenset({"Adventurer Plate", "Portraits"})),
    ("character/adventure-plate/", frozenset({"Adventurer Plate", "Portraits"})),
    ("character/companion/barding", frozenset({"Bardings"})),
    ("character/relic-gear/resplendent-tools", frozenset({"Relic Tools"})),
)

# Some freeform desktop buckets can contain labels that collide with tracked
# collectible names; keep them from inflating those logs.
_BUCKET_DISALLOWED_SHEETS: dict[str, frozenset[str]] = {
    "custom": frozenset({
        "Minions",
        "Dungeons",
        "Trials",
        "Raids",
        "Guildhests",
        "V&C Dungeon Finder",
    }),
    "quest": frozenset({
        "Dungeons",
        "Trials",
        "Raids",
        "Guildhests",
        "V&C Dungeon Finder",
    }),
}

_CRAFTING_SHARED_SHEET = "Shared Craft Log"
_CRAFTING_SHARED_FAMILY_SHEETS: tuple[frozenset[str], ...] = (
    frozenset({"Carpentry Log", "Leatherworking Log", "Weaving Log"}),
    frozenset({"Armorcrafting Log", "Blacksmithing Log", "Goldsmithing Log"}),
)


def _filter_hits_for_bucket(
    bucket: str,
    hits: list[tuple[str, int, str]] | None,
) -> list[tuple[str, int, str]] | None:
    if not hits:
        return hits

    bucket_norm = str(bucket or "").strip().casefold()
    allowed_sheets: frozenset[str] | None = None
    for prefix, sheets in _BUCKET_ALLOWED_SHEETS_PREFIXES:
        if bucket_norm.startswith(prefix):
            allowed_sheets = sheets
            break

    if not allowed_sheets:
        filtered = hits
    else:
        filtered = [
            entry
            for entry in hits
            if str(entry[0]) in allowed_sheets
        ]

    disallowed_sheets = _BUCKET_DISALLOWED_SHEETS.get(bucket_norm)
    if disallowed_sheets:
        filtered = [
            entry
            for entry in filtered
            if str(entry[0]) not in disallowed_sheets
        ]

    return filtered or None


def _parse_place_rank(label: str) -> tuple[str, int] | None:
    value = label.strip()
    if not value:
        return None

    m = re.match(r"^@PLACE\.([A-Z_']+)\s+(\d+)$", value, flags=re.IGNORECASE)
    if m:
        zone = m.group(1).replace("_", " ")
        rank = int(m.group(2))
        zone_key = _norm_lookup_key(zone)
        return _PLACE_ZONE_NORM_ALIASES.get(zone_key, zone_key), rank

    m = re.match(r"^([A-Za-z'\-\s]+)\s+(\d+)$", value)
    if m:
        zone = m.group(1).strip()
        rank = int(m.group(2))
        zone_key = _norm_lookup_key(zone)
        return _PLACE_ZONE_NORM_ALIASES.get(zone_key, zone_key), rank
    return None


def _parse_current_index(label: str) -> int | None:
    value = label.strip()
    if not value:
        return None

    patterns = (
        r"^@TRAVEL\.(?:COMPASS|QUEST)_CURRENT\s+(\d+)$",
        r"^(?:Compass|Quest|Aether)\s+Current\s+(\d+)$",
    )
    for pat in patterns:
        m = re.match(pat, value, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _aether_zone_from_path(path_parts: tuple[str, ...]) -> str | None:
    parts = [str(part).strip().casefold() for part in path_parts if str(part).strip()]
    for idx in range(len(parts) - 3):
        if parts[idx] == "travel" and parts[idx + 1] == "aether-currents":
            zone_raw = parts[idx + 3].replace("-", " ").replace("_", " ")
            return _norm_lookup_key(zone_raw)
    return None


# Source society tokens are spelled slightly differently from the workbook's
# tribe section headers; normalise the known divergences.
_SOCIETY_TOKEN_ALIASES = {
    "sylphs": "sylph",
}

_SOCIETY_RANK_RE = re.compile(
    r"^@SOCIETY\.([A-Z0-9_']+)\s*-\s*REPUTATION\.([A-Z0-9_']+)$",
    flags=re.IGNORECASE,
)


def _parse_society_rank(label: str) -> tuple[str, str] | None:
    """Parse a societal-relations name token into (tribe_key, rank_key).

    e.g. "@SOCIETY.AMALJAA - REPUTATION.NEUTRAL" -> ("amaljaa", "neutral").
    Reputation rank names repeat across every allied society, so these entries
    must be keyed by (tribe, rank) rather than matched by label alone.
    """
    m = _SOCIETY_RANK_RE.match(label.strip())
    if not m:
        return None
    tribe = _norm_lookup_key(m.group(1).replace("_", " "))
    rank = _norm_lookup_key(m.group(2).replace("_", " "))
    if not tribe or not rank:
        return None
    tribe = _SOCIETY_TOKEN_ALIASES.get(tribe, tribe)
    return tribe, rank


_HUNTING_CLASS_TOKEN_ALIASES = {
    "gla": "gladiator",
    "mrd": "marauder",
    "pgl": "pugilist",
    "lnc": "lancer",
    "arc": "archer",
    "thm": "thaumaturge",
    "cnj": "conjurer",
    "rog": "rogue",
    "acn": "arcanist",
}

_HUNTING_SOCIETY_TOKEN_ALIASES = {
    "adder": "twinadder",
    "twinadder": "twinadder",
    "orderofthetwinadder": "twinadder",
    "flames": "immortalflames",
    "immortalflames": "immortalflames",
    "maelstrom": "maelstrom",
    "themaelstrom": "maelstrom",
}

_HUNTING_SOURCE_TOKEN_RE = re.compile(
    r"^@(CLASS_JOB|SOCIETY)\.([A-Z0-9_']+)\s+(\d+)$",
    flags=re.IGNORECASE,
)

_HUNTING_WORKBOOK_LABEL_RE = re.compile(r"^([A-Za-z'\-\s]+)\s+(\d+)$")


def _classes_jobs_label_aliases(raw: str) -> list[str]:
    """Generate robust label aliases for Classes-Jobs matching.

    Workbook and desktop resources occasionally diverge in display formatting
    (e.g. "Scholar" vs "Scholar / Arcanist", or "Blue Mage" vs
    "Blue Mage (Limited Job)").
    """
    value = str(raw or "").strip()
    if not value:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        text = candidate.strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    add(value)

    without_suffix = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    if without_suffix and without_suffix.casefold() != value.casefold():
        add(without_suffix)

    # Add split aliases for dual labels like "Scholar / Arcanist".
    for part in re.split(r"\s*/\s*", without_suffix or value):
        if part.strip():
            add(part)

    # Blue Mage is commonly labelled with/without the limited-job suffix.
    if without_suffix and "blue mage" in without_suffix.casefold():
        add(f"{without_suffix} (Limited Job)")

    return out


def _parse_hunting_source_label(label: str) -> tuple[str, int] | None:
    """Parse tokenized hunting source labels into (family_key, rank).

    Examples:
      - "@CLASS_JOB.GLA 14" -> ("gladiator", 14)
      - "@SOCIETY.ADDER 01" -> ("twinadder", 1)
    """
    m = _HUNTING_SOURCE_TOKEN_RE.match(label.strip())
    if not m:
        return None

    token_type = m.group(1).casefold()
    token = _norm_lookup_key(m.group(2).replace("_", " "))
    if not token:
        return None

    if token_type == "class_job":
        family = _HUNTING_CLASS_TOKEN_ALIASES.get(token, token)
    else:
        family = _HUNTING_SOCIETY_TOKEN_ALIASES.get(token, token)

    if not family:
        return None

    return family, int(m.group(3))


def _parse_hunting_workbook_label(label: str) -> tuple[str, int] | None:
    """Parse workbook hunting labels like "Gladiator 14"."""
    m = _HUNTING_WORKBOOK_LABEL_RE.match(label.strip())
    if not m:
        return None
    family = _norm_lookup_key(m.group(1))
    if not family:
        return None
    return family, int(m.group(2))


# bucket_tail -> workbook sheet whose trackable rows line up 1:1, in order,
# with the source's ordered positional list. Matched positionally because the
# labels cannot be matched reliably: GC rank rows are labelled by seal-cap
# entitlement rather than rank name; companion skills repeat names across the
# Defender/Attacker/Healer roles (the source disambiguates with "(1)/(2)/..."
# suffixes the workbook lacks); companion ranks are bare numbers that collide
# with other rank ladders.
_POSITIONAL_SHEET_BUCKETS = {
    "grand-company-rank": "Grand Company Ranks",
    "companion-skills": "Companion Skills",
    "companion-rank": "Companion Rank",
}

# Buckets matched only by a dedicated positional/keyed handler. If that handler
# does not resolve, the candidate is left unmatched rather than falling through
# to generic label matching -- their source labels are either generic (rank
# names repeated across groups), numbered duplicates, or cross-reference
# -polluted, so a generic match would mark the wrong row.
_EXCLUSIVE_MATCH_BUCKET_TAILS = set(_POSITIONAL_SHEET_BUCKETS) | {"societal-relations"}


def _build_blue_mage_log_position_index(
    resource_root: Path,
    blue_rows: list[tuple[str, int, str]],
) -> dict[tuple[str, str], list[tuple[str, int, str]]]:
    out: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    base = resource_root / "character" / "blue-mage" / "log"
    plan = (
        ("character/blue-mage/log/dungeon", "dungeon.json"),
        ("character/blue-mage/log/trial", "trial.json"),
        ("character/blue-mage/log/raid", "raid.json"),
    )

    ids_by_bucket: dict[str, list[str]] = {}
    for bucket, filename in plan:
        path = base / filename
        if not path.exists() or not path.is_file():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        tasks = doc.get("tasks") if isinstance(doc, dict) else None
        if not isinstance(tasks, dict):
            return {}
        task_ids = [
            (_normalize_numeric_id(key), key)
            for key in tasks.keys()
        ]
        normalized = [
            (norm_id, raw_key)
            for norm_id, raw_key in task_ids
            if norm_id is not None
        ]
        normalized.sort(key=lambda item: int(item[1].lstrip("xX")))
        ids_by_bucket[bucket] = [norm_id for norm_id, _ in normalized]

    expected = sum(len(v) for v in ids_by_bucket.values())
    if expected != len(blue_rows):
        return {}

    offset = 0
    for bucket, _filename in plan:
        source_ids = ids_by_bucket[bucket]
        for pos, source_id in enumerate(source_ids):
            row_entry = blue_rows[offset + pos]
            out[(bucket, source_id)] = [row_entry]
        offset += len(source_ids)
    return out


def _index_labels_for_global(
    *,
    node_label: str,
    row_json_obj: dict[str, Any] | None,
) -> set[str]:
    labels: set[str] = set()
    value = (node_label or "").strip()
    if value:
        labels.add(value)

    if not isinstance(row_json_obj, dict):
        return labels

    for field_value in row_json_obj.values():
        if isinstance(field_value, str):
            text = field_value.strip()
            if text and len(text) <= 160:
                labels.add(text)
            continue

        if isinstance(field_value, list):
            for item in field_value:
                if isinstance(item, str):
                    text = item.strip()
                    if text and len(text) <= 160:
                        labels.add(text)

    return labels


def _dedupe_hits(hits: list[tuple[str, int, str]] | None) -> list[tuple[str, int, str]] | None:
    if not hits:
        return hits
    out: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    for sheet_name, row_index, row_type in hits:
        item = (str(sheet_name), int(row_index), str(row_type))
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _filter_adventure_plate_hits_by_sections(
    *,
    bucket: str,
    source_labels: list[str] | tuple[str, ...],
    hits: list[tuple[str, int, str]] | None,
    row_sections: dict[tuple[str, int, str], str],
) -> list[tuple[str, int, str]] | None:
    if not hits:
        return hits
    if not str(bucket or "").startswith("character/adventure-plate"):
        return hits

    source_sections = _adventure_plate_sections_from_labels(source_labels)
    if not source_sections:
        return hits

    filtered = [
        entry
        for entry in hits
        if row_sections.get((str(entry[0]), int(entry[1]), str(entry[2]))) in source_sections
    ]
    # Fall back to original hits if section metadata unexpectedly fails to map.
    return filtered or hits


def _row_text_tokens(value: Any) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    out: set[str] = set()
    compact = _norm_lookup_key(text)
    if compact:
        out.add(compact)
    for token in _norm_label(text).split():
        t = token.strip()
        if t:
            out.add(t)
    return out


def _hit_tokens(
    entry: tuple[str, int, str],
    row_context: dict[tuple[str, int, str], dict[str, Any]],
) -> set[str]:
    data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
    out: set[str] = set()
    out.update(_row_text_tokens(data.get("sheet_name")))
    out.update(_row_text_tokens(data.get("section_label")))
    out.update(_row_text_tokens(data.get("label")))
    row_json_obj = data.get("row_json_obj")
    if isinstance(row_json_obj, dict):
        for field in (
            "quest",
            "npc",
            "unlocks",
            "unlocked_by",
            "trial",
            "dungeon",
            "item",
            "type",
            "building",
            "requires",
        ):
            value = row_json_obj.get(field)
            if isinstance(value, str):
                out.update(_row_text_tokens(value))
    return out


def _filter_quest_hits_by_source_tokens(
    *,
    bucket: str,
    source_labels: list[str] | tuple[str, ...],
    hits: list[tuple[str, int, str]] | None,
    row_context: dict[tuple[str, int, str], dict[str, Any]],
    starting_class: str | None,
) -> list[tuple[str, int, str]] | None:
    if not hits or bucket != "quest":
        return hits

    source_tokens = _quest_path_tokens_from_labels(source_labels)
    if source_tokens:
        generic_tokens = {
            "duty",
            "quest",
            "sidequests",
            "levequests",
            "mainscenario",
            "otherquests",
            "main",
            "scenario",
            "quests",
            "the",
            "la",
            "noscea",
            "leves",
            "seventhumbraleramainscenarioquests",
        }
        meaningful_tokens = {token for token in source_tokens if token not in generic_tokens}
        if meaningful_tokens:
            scored: list[tuple[tuple[str, int, str], int]] = []
            best_score = 0
            for entry in hits:
                overlap = _hit_tokens(entry, row_context).intersection(meaningful_tokens)
                score = len(overlap)
                if score <= 0:
                    continue
                scored.append((entry, score))
                if score > best_score:
                    best_score = score

            if scored and best_score > 0:
                hits = [entry for entry, score in scored if score == best_score]

    # Starting class is a tiebreaker only after source-path filtering. This
    # keeps city-specific source paths (e.g. Limsa/Ul'dah variants) pinned to
    # their own rows so excluded states can be applied correctly.
    if hits and len(hits) > 1:
        city_key = _starting_city_for_class(starting_class)
        city_tokens = _STARTING_CITY_SOURCE_TOKENS.get(city_key, set()) if city_key else set()
        if city_tokens:
            city_filtered = [
                entry
                for entry in hits
                if _hit_tokens(entry, row_context).intersection(city_tokens)
            ]
            if city_filtered:
                hits = city_filtered

    return hits


def _filter_hits_by_unlock_field(
    *,
    bucket: str,
    match_labels: list[str],
    hits: list[tuple[str, int, str]] | None,
    row_context: dict[tuple[str, int, str], dict[str, Any]],
) -> list[tuple[str, int, str]] | None:
    if not hits:
        return hits
    if bucket != "quest" and not bucket.startswith("duty/collection"):
        return hits
    if not match_labels:
        return hits

    label_norm = _norm_lookup_key(match_labels[0])
    if not label_norm:
        return hits

    filtered: list[tuple[str, int, str]] = []
    for entry in hits:
        data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
        row_json_obj = data.get("row_json_obj")
        if not isinstance(row_json_obj, dict):
            continue
        for field in ("unlocked_by", "unlocks"):
            value = row_json_obj.get(field)
            if isinstance(value, str) and _norm_lookup_key(value) == label_norm:
                filtered.append(entry)
                break
    return filtered or hits


def _filter_fate_hits(
    *,
    bucket: str,
    hits: list[tuple[str, int, str]] | None,
) -> list[tuple[str, int, str]] | None:
    if not hits or not bucket.startswith("duty/fate/"):
        return hits
    filtered = [entry for entry in hits if "fate" in str(entry[0]).casefold()]
    return filtered or hits


def _filter_gathering_log_hits_by_type(
    *,
    bucket: str,
    hits: list[tuple[str, int, str]] | None,
    row_context: dict[tuple[str, int, str], dict[str, Any]],
) -> list[tuple[str, int, str]] | None:
    if not hits or not bucket.startswith("logs/gathering/gathering-log/"):
        return hits

    expected_type = {
        "logging": "logging",
        "harvesting": "harvesting",
        "mining": "mining",
        "quarrying": "quarrying",
    }.get(_bucket_tail(bucket))
    if not expected_type:
        return hits

    filtered: list[tuple[str, int, str]] = []
    for entry in hits:
        data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
        row_json_obj = data.get("row_json_obj")
        if not isinstance(row_json_obj, dict):
            continue
        row_type = _norm_lookup_key(str(row_json_obj.get("type") or ""))
        if row_type == expected_type:
            filtered.append(entry)
    return filtered or hits


def _filter_crafting_log_hits(
    *,
    bucket: str,
    match_labels: Sequence[str],
    hits: list[tuple[str, int, str]] | None,
    row_context: dict[tuple[str, int, str], dict[str, Any]],
) -> list[tuple[str, int, str]] | None:
    if not hits or not bucket.startswith("logs/crafting-log/"):
        return hits

    expanded_labels: list[str] = []
    seen_expanded: set[str] = set()
    for label in match_labels or ():
        text = str(label).strip()
        if not text:
            continue
        key = text.casefold()
        if key not in seen_expanded:
            seen_expanded.add(key)
            expanded_labels.append(text)
        for alias in _crafting_label_aliases(bucket, text):
            alias_key = alias.casefold()
            if alias_key in seen_expanded:
                continue
            seen_expanded.add(alias_key)
            expanded_labels.append(alias)

    alias_norms = {
        _norm_label(str(label))
        for label in expanded_labels
        if str(label).strip()
    }
    alias_lookup_norms = {
        _norm_lookup_key(str(label))
        for label in expanded_labels
        if str(label).strip()
    }
    if not alias_norms and not alias_lookup_norms:
        return hits

    expected_stat = _expected_crafting_alkahest_stat(match_labels)
    base_name_norms = {
        _norm_label(base)
        for base in (_crafting_stat_affix_base_name(label) for label in (match_labels or []))
        if base
    }

    filtered: list[tuple[str, int, str]] = []
    for entry in hits:
        data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
        label_text = str(data.get("label") or "") if isinstance(data, dict) else ""
        row_json_obj = data.get("row_json_obj") if isinstance(data, dict) else None
        item_text = ""
        if isinstance(row_json_obj, dict):
            item_text = str(row_json_obj.get("item") or "")

        candidates_norm = {_norm_label(label_text), _norm_label(item_text)}
        candidates_lookup = {_norm_lookup_key(label_text), _norm_lookup_key(item_text)}

        if candidates_norm.intersection(alias_norms) or candidates_lookup.intersection(alias_lookup_norms):
            filtered.append(entry)

    if filtered and expected_stat:
        stat_filtered = _filter_crafting_hits_by_expected_stat(filtered, row_context, expected_stat)
        if stat_filtered:
            return stat_filtered

    if filtered:
        return filtered

    if expected_stat and base_name_norms:
        base_hits: list[tuple[str, int, str]] = []
        for entry in hits:
            data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
            label_text = str(data.get("label") or "") if isinstance(data, dict) else ""
            row_json_obj = data.get("row_json_obj") if isinstance(data, dict) else None
            item_text = ""
            if isinstance(row_json_obj, dict):
                item_text = str(row_json_obj.get("item") or "")

            if _norm_label(label_text) in base_name_norms or _norm_label(item_text) in base_name_norms:
                base_hits.append(entry)

        stat_filtered = _filter_crafting_hits_by_expected_stat(base_hits, row_context, expected_stat)
        if stat_filtered:
            return stat_filtered

    # If only ingredient-side text matched, treat it as unresolved instead of faning out.
    return []


def _crafting_stat_affix_base_name(label: str) -> str | None:
    value = str(label or "").strip()
    if not value:
        return None
    m = re.match(
        r"^(.*?\bof)\s+(fending|striking|aiming|casting|healing)\b",
        value,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    base = m.group(1).strip()
    return base or None


def _expected_crafting_alkahest_stat(match_labels: Sequence[str]) -> str | None:
    for label in match_labels or ():
        norm = _norm_label(str(label))
        if not norm:
            continue
        for affix, stat in _CRAFTING_ALKAHEST_STAT_BY_AFFIX.items():
            token = f" of {affix}"
            if token in norm or norm.endswith(f" {affix}"):
                return stat
    return None


def _filter_crafting_hits_by_expected_stat(
    hits: Sequence[tuple[str, int, str]],
    row_context: dict[tuple[str, int, str], dict[str, Any]],
    expected_stat: str,
) -> list[tuple[str, int, str]]:
    expected_key = _norm_lookup_key(expected_stat)
    if not expected_key:
        return []

    out: list[tuple[str, int, str]] = []
    for entry in hits:
        data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
        row_json_obj = data.get("row_json_obj") if isinstance(data, dict) else None
        if not isinstance(row_json_obj, dict):
            continue
        mat_3_key = _norm_lookup_key(str(row_json_obj.get("mat_3") or row_json_obj.get("mat3") or ""))
        if expected_key and expected_key in mat_3_key:
            out.append((str(entry[0]), int(entry[1]), str(entry[2])))

    return out


def _is_safe_crafting_shared_family_hitset(
    hits: list[tuple[str, int, str]] | None,
) -> bool:
    if not hits or len(hits) < 2:
        return False

    sheet_names = {str(entry[0]) for entry in hits}
    if _CRAFTING_SHARED_SHEET in sheet_names:
        return False

    for family in _CRAFTING_SHARED_FAMILY_SHEETS:
        if sheet_names != family:
            continue
        if all(sum(1 for entry in hits if str(entry[0]) == sheet_name) == 1 for sheet_name in family):
            return True
    return False


def _remap_crafting_log_cross_bucket_hits(
    *,
    bucket: str,
    aliases: Sequence[str],
    match_labels: Sequence[str],
    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]],
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]],
    row_context: dict[tuple[str, int, str], dict[str, Any]],
) -> list[tuple[str, int, str]] | None:
    if not bucket.startswith("logs/crafting-log/"):
        return None
    if not aliases:
        return None

    out: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()

    for other_bucket, bucket_exact in exact_idx.items():
        if other_bucket == bucket:
            continue
        if not other_bucket.startswith("logs/crafting-log/"):
            continue

        bucket_norm = norm_idx.get(other_bucket, {})
        for alias in aliases:
            for entry in bucket_exact.get(alias.casefold(), []):
                key = (str(entry[0]), int(entry[1]), str(entry[2]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)

            norm = _norm_label(alias)
            if not norm:
                continue
            for entry in bucket_norm.get(norm, []):
                key = (str(entry[0]), int(entry[1]), str(entry[2]))
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)

    hits = _dedupe_hits(out)
    hits = _dedupe_hits(
        _filter_crafting_log_hits(
            bucket=bucket,
            match_labels=match_labels,
            hits=hits,
            row_context=row_context,
        )
    )
    if not hits:
        return None

    sheet_names = {str(entry[0]) for entry in hits}

    # Job buckets may safely fall back to a unique Shared Craft row.
    if bucket != "logs/crafting-log/shared":
        if sheet_names == {_CRAFTING_SHARED_SHEET} and len(hits) == 1:
            return hits
        return None

    # Shared bucket can safely fan out only for known one-per-sheet triads.
    if _is_safe_crafting_shared_family_hitset(hits):
        return sorted(hits, key=lambda entry: (str(entry[0]), int(entry[1]), str(entry[2])))
    return None


def _select_progression_hit(
    *,
    bucket: str,
    match_labels: list[str],
    hits: list[tuple[str, int, str]] | None,
) -> list[tuple[str, int, str]] | None:
    if not hits:
        return hits
    if bucket != "achievement":
        return hits
    if not match_labels:
        return hits

    source_label = match_labels[0]
    m = re.search(r"\b(\d{1,2}|I|II|III|IV|V|VI|VII|VIII|IX|X)$", source_label.strip(), flags=re.IGNORECASE)
    if not m:
        return hits

    token = m.group(1).upper()
    if token.isdigit():
        index = int(token)
    else:
        index = _ROMAN_MAP.get(token)
    if not index or index <= 0:
        return hits

    ordered = sorted(hits, key=lambda entry: int(entry[1]))
    if index > len(ordered):
        return hits
    return [ordered[index - 1]]


def _collapse_duplicate_signature_hits(
    *,
    bucket: str,
    hits: list[tuple[str, int, str]] | None,
    row_context: dict[tuple[str, int, str], dict[str, Any]],
) -> list[tuple[str, int, str]] | None:
    if not hits or len(hits) <= 1:
        return hits
    if bucket == "duty/island-sanctuary/buildings":
        return hits

    out: list[tuple[str, int, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in sorted(hits, key=lambda item: (str(item[0]), int(item[1]), str(item[2]))):
        data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
        row_json_obj = data.get("row_json_obj") if isinstance(data, dict) else None
        if isinstance(row_json_obj, dict):
            row_json_sig = json.dumps(row_json_obj, sort_keys=True, ensure_ascii=True)
        else:
            row_json_sig = "{}"
        sig = (
            str(data.get("sheet_name") or entry[0]),
            str(data.get("section_label") or ""),
            str(data.get("label") or ""),
            row_json_sig,
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append(entry)

    return out or hits


def _filter_island_sanctuary_hits(
    *,
    bucket: str,
    hits: list[tuple[str, int, str]] | None,
    source_state: str,
    source_value: float | None,
) -> list[tuple[str, int, str]] | None:
    if not hits or not bucket.startswith("duty/island-sanctuary/"):
        return hits

    bucket_sheets = {
        "duty/island-sanctuary/animals": frozenset({
            "Island Sanctuary - Rare Animals",
            "Island Sanctuary - Animals",
        }),
        "duty/island-sanctuary/buildings": frozenset({"Island Sanctuary - Buildings"}),
        "duty/island-sanctuary/crafting": frozenset({"Island Sanctuary - Crafting"}),
        "duty/island-sanctuary/isleventory": frozenset({"Island Sanctuary - Isleventory"}),
        "duty/island-sanctuary/rank": frozenset({"Island Sanctuary - Rank"}),
    }.get(bucket)

    filtered = hits
    if bucket_sheets:
        filtered = [entry for entry in hits if str(entry[0]) in bucket_sheets]

    if bucket == "duty/island-sanctuary/buildings" and source_state == "value":
        count = int(max(0, round(float(source_value or 0.0))))
        if count <= 0:
            return []
        filtered = sorted(filtered, key=lambda entry: int(entry[1]))[:count]

    return filtered


def _allows_multi_hit_candidate(
    *,
    bucket: str,
    source_labels: list[str] | tuple[str, ...],
    match_labels: list[str] | None = None,
    hits: list[tuple[str, int, str]] | None = None,
    row_context: dict[tuple[str, int, str], dict[str, Any]] | None = None,
) -> bool:
    bucket_value = str(bucket or "")
    if bucket_value.startswith("character/adventure-plate"):
        return bool(_adventure_plate_sections_from_labels(source_labels))

    if bucket_value == "duty/island-sanctuary/buildings":
        return bool(hits)

    if bucket_value == "logs/crafting-log/shared":
        return _is_safe_crafting_shared_family_hitset(hits)

    if (bucket_value == "quest" or bucket_value.startswith("duty/collection")) and hits and row_context:
        labels = match_labels or _candidate_match_labels(source_labels)
        if not labels:
            return False
        label_norm = _norm_lookup_key(labels[0])
        if not label_norm:
            return False
        for entry in hits:
            data = row_context.get((str(entry[0]), int(entry[1]), str(entry[2])), {})
            row_json_obj = data.get("row_json_obj")
            if not isinstance(row_json_obj, dict):
                return False
            unlocked_by = row_json_obj.get("unlocked_by")
            unlocks = row_json_obj.get("unlocks")
            if not (
                (isinstance(unlocked_by, str) and _norm_lookup_key(unlocked_by) == label_norm)
                or (isinstance(unlocks, str) and _norm_lookup_key(unlocks) == label_norm)
            ):
                return False
        return True

    return False


def _partial_match_hits_generic(
    aliases: list[str],
    norm_index: dict[str, list[tuple[str, int, str]]],
    *,
    cutoff: float = 0.92,
) -> list[tuple[str, int, str]] | None:
    if not norm_index:
        return None

    norm_keys = list(norm_index.keys())
    for alias in aliases:
        norm = _norm_label(alias)
        if len(norm) < 8:
            continue
        close = difflib.get_close_matches(norm, norm_keys, n=1, cutoff=cutoff)
        if close:
            hits = norm_index.get(close[0])
            if hits:
                return hits
    return None


def _generic_label_aliases(raw: str) -> set[str]:
    value = raw.strip()
    if not value:
        return set()

    aliases: set[str] = {value}

    if value.startswith("@"):
        token_text = value[1:]
        token_text = re.sub(r"[._]+", " ", token_text)
        token_text = re.sub(r"\s+", " ", token_text).strip()
        if token_text:
            pretty = token_text.title()
            aliases.add(pretty)

            short = re.sub(
                r"^(PLACE|TRAVEL|SOCIETY|REPUTATION|EXPANSION|HEADER)\s+",
                "",
                token_text,
                flags=re.IGNORECASE,
            ).strip()
            if short:
                aliases.add(short.title())

            if " - " in token_text:
                left, right = [p.strip() for p in token_text.split(" - ", 1)]
                left = re.sub(
                    r"^(SOCIETY|PLACE|EXPANSION|TRAVEL)\s+",
                    "",
                    left,
                    flags=re.IGNORECASE,
                ).strip()
                right = re.sub(
                    r"^(REPUTATION|RANK)\s+",
                    "",
                    right,
                    flags=re.IGNORECASE,
                ).strip()
                if left and right:
                    aliases.add(f"{left.title()} {right.title()}")
                if left:
                    aliases.add(left.title())
                if right:
                    aliases.add(right.title())

    if " - " in value:
        right = value.split(" - ", 1)[1].strip()
        if right:
            aliases.add(right)

    if re.search(r"(?i)compass current|quest current", value):
        aliases.add(re.sub(r"(?i)compass current|quest current", "Aether Current", value))

    # Desktop resources use "Goobbue" while workbook has at least one
    # island-sanctuary row labeled "Goobue".
    if re.search(r"(?i)\bgoobbue\b", value):
        aliases.add(re.sub(r"(?i)\bgoobbue\b", "Goobue", value))
    if re.search(r"(?i)\bgoobue\b", value):
        aliases.add(re.sub(r"(?i)\bgoobue\b", "Goobbue", value))

    m_duty = re.fullmatch(r"[dD]\.(\d+)", value)
    if m_duty:
        duty_num = str(int(m_duty.group(1)))
        aliases.add(f"Duty {duty_num}")
        aliases.add(duty_num)

    return {alias for alias in aliases if alias.strip()}


def _decode_completion_value(raw: Any) -> tuple[str, float | None] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if text == "Y":
            return ("done", None)
        if text == "X":
            return ("excluded", None)
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            pct = max(0.0, float(text))
            return ("value", pct)
        return None

    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        pct = max(0.0, float(raw))
        return ("value", pct)

    return None


def _walk_leaves(node: Any, path: tuple[str, ...] = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield from _walk_leaves(value, path + (key,))
    else:
        yield path, node


def _merge_source_state(
    current_state: str,
    current_value: float | None,
    new_state: str,
    new_value: float | None,
) -> tuple[str, float | None]:
    priority = {"excluded": 1, "value": 2, "done": 3}
    if priority[new_state] > priority[current_state]:
        return new_state, new_value
    if priority[new_state] < priority[current_state]:
        return current_state, current_value
    if new_state == "value":
        return "value", max(float(current_value or 0.0), float(new_value or 0.0))
    return current_state, current_value


def _merge_row_action(
    row_actions: dict[tuple[str, int], dict[str, Any]],
    key: tuple[str, int],
    *,
    row_type: str,
    state: str,
    value: float | None,
) -> None:
    existing = row_actions.get(key)
    if existing is None:
        row_actions[key] = {
            "row_type": row_type,
            "state": state,
            "value": value,
        }
        return

    merged_state, merged_value = _merge_source_state(
        str(existing.get("state") or "excluded"),
        existing.get("value") if isinstance(existing.get("value"), (int, float)) else None,
        state,
        value,
    )
    existing["state"] = merged_state
    existing["value"] = merged_value
    existing["row_type"] = row_type


def load_completion_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Desktop completion payload must be a JSON object")
    overall = data.get("overall")
    if not isinstance(overall, dict):
        raise ValueError("Desktop completion payload is missing an object 'overall' section")
    return data


# The desktop app and this workbook share the same five top-level menu
# sections. Anchoring the cross-sheet (global) match fallback to the source
# item's section prevents a generic name from leaking across sections (e.g. a
# Duty quest marking a Logs row complete).
_COMPLETION_TOP_SECTIONS = ("character", "duty", "logs", "travel", "social")

_TOP_MENU_SHEET_TO_SECTION = {
    "Character Menu": "character",
    "Duty Menu": "duty",
    "Logs Menu": "logs",
    "Travel Menu": "travel",
    "Social Menu": "social",
}


def _completion_top_section(path_parts: tuple[str, ...] | list[str]) -> str | None:
    """Return the top-level section (character/duty/...) a completion path
    belongs to, e.g. ('overall', 'duty', 'quest', ...) -> 'duty'."""
    for part in path_parts:
        p = str(part).strip().casefold()
        if p in _COMPLETION_TOP_SECTIONS:
            return p
    return None


def _build_sheet_section_map(
    conn: sqlite3.Connection, run_id: int
) -> dict[str, str | None]:
    """Map every sheet to its top-level menu section by walking parent_sheet
    up to one of the five top menus."""
    parent: dict[str, str | None] = {}
    for row in conn.execute(
        "SELECT sheet_name, parent_sheet FROM sheets WHERE run_id = ?",
        (run_id,),
    ):
        parent[row["sheet_name"]] = row["parent_sheet"]

    def resolve(sheet: str) -> str | None:
        seen: set[str] = set()
        cur: str | None = sheet
        while cur and cur not in seen:
            seen.add(cur)
            section = _TOP_MENU_SHEET_TO_SECTION.get(cur)
            if section:
                return section
            cur = parent.get(cur)
        return None

    return {sheet: resolve(sheet) for sheet in parent}


def import_desktop_completion(
    conn: sqlite3.Connection,
    *,
    character_id: int,
    completion_path: Path,
    clear_existing: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ImportSummary:
    def log(message: str) -> None:
        if progress is not None:
            progress(message)

    run_id = db.latest_run_id(conn)
    if run_id is None:
        raise ValueError("No ingest run found. Run scripts/prep_xlsx_to_sqlite.py first.")

    character = db.get_character(conn, character_id)
    if character is None:
        raise ValueError(f"Character id {character_id} was not found")

    log(f"Loading desktop completion payload: {completion_path}")
    payload = load_completion_payload(completion_path)
    payload_starting_class = _completion_payload_starting_class(payload)
    effective_starting_class = payload_starting_class or character["starting_class"]
    if payload_starting_class and payload_starting_class != character["starting_class"]:
        log(
            "Using starting class from completion payload: "
            f"{payload_starting_class} (character default: {character['starting_class']})"
        )

    resource_root = resolve_resource_root()
    if resource_root is None:
        raise ValueError(
            "Could not find desktop app resources under LOCALAPPDATA. "
            "Expected LOCALAPPDATA/Programs/ffxiv-completionist/resources/resources"
        )

    source_index = _build_source_label_index(str(resource_root))

    inline_source_index = _build_inline_completion_source_index(payload)
    if inline_source_index:
        source_index = _merge_source_indexes(source_index, inline_source_index)

    indexed_count = sum(len(v) for v in source_index.values())
    log(f"Loaded desktop source index from {resource_root} ({indexed_count} ids)")
    if inline_source_index:
        inline_count = sum(len(v) for v in inline_source_index.values())
        log(f"Merged {inline_count} inline source ids from completion payload metadata")

    if clear_existing:
        log("Clearing existing character progress before import")
        reset_character_progress(conn, character, run_id)

    # Aggregate by source id so duplicate leaves in alternate branches collapse deterministically.
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    missing_source_ids: dict[tuple[str, str], dict[str, Any]] = {}
    supported_entries = 0

    for path_parts, raw_value in _walk_leaves(payload.get("overall", {}), ("overall",)):
        if not path_parts:
            continue
        leaf_id = _normalize_numeric_id(path_parts[-1])
        if leaf_id is None:
            continue

        bucket = _completion_bucket_from_path(path_parts)
        if bucket is None:
            continue

        state_info = _decode_completion_value(raw_value)
        if state_info is None:
            continue

        supported_entries += 1
        state, pct = state_info
        labels, source_bucket = _lookup_source_labels(
            source_index,
            bucket=bucket,
            source_id=leaf_id,
        )
        if not labels and _bucket_tail(bucket) not in _POSITIONAL_VALUE_BUCKETS:
            missing_key = (bucket, leaf_id)
            existing_missing = missing_source_ids.get(missing_key)
            if existing_missing is None:
                missing_source_ids[missing_key] = {
                    "bucket": bucket,
                    "label": f"id:{leaf_id}",
                    "source_id": leaf_id,
                    "source_state": state,
                    "reason": "id_not_in_source_index",
                    "value": pct,
                }
            else:
                merged_state, merged_value = _merge_source_state(
                    str(existing_missing.get("source_state") or "excluded"),
                    (
                        float(existing_missing["value"])
                        if isinstance(existing_missing.get("value"), (int, float))
                        else None
                    ),
                    state,
                    pct,
                )
                existing_missing["source_state"] = merged_state
                existing_missing["value"] = merged_value
            continue

        key = (bucket, leaf_id)
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = {
                "bucket": bucket,
                "source_bucket": source_bucket,
                "source_id": leaf_id,
                "source_state": state,
                "value": pct,
                "labels": list(labels) if labels else [],
                "source_path_parts": [str(part) for part in path_parts],
            }
            continue

        merged_state, merged_value = _merge_source_state(
            str(existing.get("source_state") or "excluded"),
            existing.get("value") if isinstance(existing.get("value"), (int, float)) else None,
            state,
            pct,
        )
        existing["source_state"] = merged_state
        existing["value"] = merged_value
        label_pool = {str(label).strip() for label in existing.get("labels", []) if isinstance(label, str)}
        label_pool.update(str(label).strip() for label in (labels or ()) if isinstance(label, str))
        existing["labels"] = sorted(label for label in label_pool if label)
        if not existing.get("source_bucket") and source_bucket:
            existing["source_bucket"] = source_bucket
        if not existing.get("source_path_parts"):
            existing["source_path_parts"] = [str(part) for part in path_parts]

    candidates = list(aggregated.values())
    unmatched_items: list[dict[str, Any]] = [
        {
            "bucket": item["bucket"],
            "label": item["label"],
            "source_id": item["source_id"],
            "source_state": item["source_state"],
            "reason": item["reason"],
        }
        for item in missing_source_ids.values()
    ]
    total_candidates = len(candidates) + len(unmatched_items)
    log(
        f"Collected {total_candidates} desktop candidates from {supported_entries} supported completion entries"
    )

    rows = conn.execute(
        """
                SELECT n.sheet_name, n.row_index, n.section_label, n.label, n.row_type, n.row_json
        FROM nodes n
        JOIN sheets s ON s.run_id = n.run_id AND s.sheet_name = n.sheet_name
        WHERE n.run_id = ?
          AND s.is_menu = 0
          AND n.label IS NOT NULL
          AND n.row_type IN ('checkbox', 'value')
        """,
        (run_id,),
    ).fetchall()

    sheet_section_map = _build_sheet_section_map(conn, run_id)

    exact_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    norm_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    # Global (cross-sheet) indexes, both unscoped and partitioned by top-level
    # section. A source item whose section is known consults only its own
    # section to avoid cross-section bleed; items with no resolvable section
    # (e.g. user "custom" entries) fall back to the unscoped index.
    global_exact_idx: dict[str, list[tuple[str, int, str]]] = {}
    global_norm_idx: dict[str, list[tuple[str, int, str]]] = {}
    global_exact_by_section: dict[str | None, dict[str, list[tuple[str, int, str]]]] = {}
    global_norm_by_section: dict[str | None, dict[str, list[tuple[str, int, str]]]] = {}
    shared_fate_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    aether_current_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    blue_mage_rows: list[tuple[str, int, str]] = []
    classes_jobs_rows: list[tuple[str, int, str]] = []
    classes_jobs_label_idx: dict[str, list[tuple[str, int, str]]] = {}
    desynthesis_rows: list[tuple[str, int, str]] = []
    hunting_idx: dict[tuple[str, int], list[tuple[str, int, str]]] = {}
    # Positional-sheet rows, collected per sheet in row order (see
    # _POSITIONAL_SHEET_BUCKETS). Matched by source index -> nth row.
    positional_sheet_rows: dict[str, list[tuple[str, int, str]]] = {
        sheet: [] for sheet in _POSITIONAL_SHEET_BUCKETS.values()
    }
    # Society reputation ranks are keyed by (tribe, rank) because rank names
    # repeat across every allied society.
    society_idx: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    adventure_plate_row_sections: dict[tuple[str, int, str], str] = {}
    row_context: dict[tuple[str, int, str], dict[str, Any]] = {}

    for row in rows:
        sheet_name = row["sheet_name"]
        label = (row["label"] or "").strip()
        if not label:
            continue

        row_json_obj: dict[str, Any] | None = None
        row_json_text = row["row_json"]
        if isinstance(row_json_text, str) and row_json_text.strip():
            try:
                decoded = json.loads(row_json_text)
                if isinstance(decoded, dict):
                    row_json_obj = decoded
            except json.JSONDecodeError:
                row_json_obj = None

        entry = (sheet_name, int(row["row_index"]), row["row_type"])
        section_label = str(row["section_label"] or "")

        row_context[entry] = {
            "sheet_name": sheet_name,
            "section_label": section_label,
            "label": label,
            "row_json_obj": row_json_obj,
        }

        if sheet_name in ("Adventurer Plate", "Portraits"):
            section_key = _adventure_plate_row_section_key(sheet_name, section_label)
            if section_key:
                adventure_plate_row_sections[entry] = section_key

        if sheet_name == "Shared FATE":
            section_key = _norm_lookup_key(section_label)
            rank_value: int | None = None
            rank_from_label = _normalize_numeric_id(label)
            if rank_from_label is not None:
                rank_value = int(rank_from_label)
            elif isinstance(row_json_obj, dict):
                rank_from_json = _normalize_numeric_id(row_json_obj.get("rank"))
                if rank_from_json is not None:
                    rank_value = int(rank_from_json)
            if section_key and rank_value is not None:
                shared_fate_idx.setdefault((section_key, rank_value), []).append(entry)

        if sheet_name == "Aether Currents" and isinstance(row_json_obj, dict):
            section_key = _norm_lookup_key(section_label)
            current_value = _normalize_numeric_id(row_json_obj.get("col_2"))
            if section_key and current_value is not None:
                aether_current_idx.setdefault((section_key, int(current_value)), []).append(entry)

        if sheet_name == "Blue Mage Log":
            blue_mage_rows.append(entry)

        if sheet_name == "Classes-Jobs" and row["row_type"] == "value":
            section_norm = _norm_label(section_label)
            if "desynthesis" in section_norm:
                desynthesis_rows.append(entry)
            else:
                classes_jobs_rows.append(entry)
                for alias in _classes_jobs_label_aliases(label):
                    norm = _norm_label(alias)
                    if norm:
                        classes_jobs_label_idx.setdefault(norm, []).append(entry)

        if sheet_name == "Hunting Logs":
            hunting_key = _parse_hunting_workbook_label(label)
            if hunting_key:
                hunting_idx.setdefault(hunting_key, []).append(entry)

        if sheet_name in positional_sheet_rows:
            positional_sheet_rows[sheet_name].append(entry)

        if sheet_name == "Society Relations":
            society_key = (_norm_lookup_key(section_label), _norm_lookup_key(label))
            if all(society_key):
                society_idx.setdefault(society_key, []).append(entry)

        row_section = sheet_section_map.get(sheet_name)
        section_exact = global_exact_by_section.setdefault(row_section, {})
        section_norm = global_norm_by_section.setdefault(row_section, {})
        for idx_label in _index_labels_for_global(
            node_label=label,
            row_json_obj=row_json_obj,
        ):
            global_exact_idx.setdefault(idx_label.casefold(), []).append(entry)
            section_exact.setdefault(idx_label.casefold(), []).append(entry)
            norm = _norm_label(idx_label)
            if norm:
                global_norm_idx.setdefault(norm, []).append(entry)
                section_norm.setdefault(norm, []).append(entry)

        for bucket in _row_buckets_for_sheet(
            sheet_name,
            section_label,
            row_json_obj=row_json_obj,
        ):
            for idx_label in _index_labels_for_bucket(
                bucket=bucket,
                node_label=label,
                row_json_obj=row_json_obj,
            ):
                exact_idx.setdefault(bucket, {}).setdefault(idx_label.casefold(), []).append(entry)
                norm = _norm_label(idx_label)
                if norm:
                    norm_idx.setdefault(bucket, {}).setdefault(norm, []).append(entry)

    norm_keys_idx: dict[str, list[str]] = {
        bucket: list(bucket_norm.keys())
        for bucket, bucket_norm in norm_idx.items()
    }
    blue_mage_rows.sort(key=lambda item: item[1])
    classes_jobs_rows.sort(key=lambda item: item[1])
    desynthesis_rows.sort(key=lambda item: item[1])
    blue_mage_idx = _build_blue_mage_log_position_index(resource_root, blue_mage_rows)
    classes_jobs_idx: dict[str, list[tuple[str, int, str]]] = {
        str(pos): [entry] for pos, entry in enumerate(classes_jobs_rows)
    }
    desynthesis_idx: dict[str, list[tuple[str, int, str]]] = {
        str(pos): [entry] for pos, entry in enumerate(desynthesis_rows)
    }
    # bucket_tail -> {str(position): [row]} positional index per configured sheet.
    positional_sheet_idx: dict[str, dict[str, list[tuple[str, int, str]]]] = {}
    for bucket_tail_key, sheet in _POSITIONAL_SHEET_BUCKETS.items():
        ordered = sorted(positional_sheet_rows.get(sheet, []), key=lambda item: item[1])
        positional_sheet_idx[bucket_tail_key] = {
            str(pos): [entry] for pos, entry in enumerate(ordered)
        }

    # --- Importer logic checks -------------------------------------------
    # Positional and keyed buckets are alignment-sensitive: if the desktop app
    # and the workbook disagree on size or order, results shift silently. Log
    # the alignment up front so drift (e.g. a game-version mismatch adding a
    # row) is visible in the import log rather than discovered by eye later.
    exclusive_bucket_counts: dict[str, int] = {}
    max_source_index: dict[str, int] = {}
    for candidate in candidates:
        tail = _bucket_tail(str(candidate.get("bucket") or ""))
        if tail not in _EXCLUSIVE_MATCH_BUCKET_TAILS:
            continue
        exclusive_bucket_counts[tail] = exclusive_bucket_counts.get(tail, 0) + 1
        sid = _normalize_numeric_id(candidate.get("source_id"))
        if sid is not None:
            max_source_index[tail] = max(max_source_index.get(tail, -1), int(sid))

    for tail, sheet in _POSITIONAL_SHEET_BUCKETS.items():
        n_src = exclusive_bucket_counts.get(tail, 0)
        if not n_src:
            continue
        capacity = len(positional_sheet_idx.get(tail, {}))
        log(f"Check [{sheet}]: {n_src} source entries vs {capacity} workbook rows (positional)")
        top = max_source_index.get(tail, -1)
        if capacity and top >= capacity:
            log(
                f"WARNING [{sheet}]: source position {top} exceeds the {capacity} workbook "
                "rows -- positional alignment is off (likely game-version/structure drift)."
            )

    if exclusive_bucket_counts.get("societal-relations"):
        log(
            f"Check [Society Relations]: {exclusive_bucket_counts['societal-relations']} source "
            f"entries vs {len(society_idx)} keyed (tribe, rank) rows"
        )

    row_actions: dict[tuple[str, int], dict[str, Any]] = {}
    matched_candidates = 0
    ignored_untracked = 0
    ambiguous_candidates = 0

    for candidate in candidates:
        bucket = str(candidate.get("bucket") or "")
        source_id = str(candidate.get("source_id") or "")
        source_path_parts = tuple(str(part) for part in candidate.get("source_path_parts") or ())
        source_state = str(candidate.get("source_state") or "done")
        source_value = (
            float(candidate["value"])
            if isinstance(candidate.get("value"), (int, float))
            else None
        )

        labels = [
            str(label).strip()
            for label in candidate.get("labels", [])
            if isinstance(label, str) and str(label).strip()
        ]
        match_labels = _candidate_match_labels(labels)

        if _is_quarantined_bucket(bucket):
            ignored_untracked += 1
            continue

        bucket_tail = _bucket_tail(bucket)
        if not match_labels and bucket_tail not in _POSITIONAL_VALUE_BUCKETS:
            unmatched_items.append({
                "bucket": bucket,
                "label": f"id:{candidate.get('source_id')}",
                "source_id": candidate.get("source_id"),
                "source_state": source_state,
                "reason": "missing_source_labels",
            })
            continue

        hits: list[tuple[str, int, str]] | None = None

        if bucket_tail == "classes-jobs":
            # Prefer explicit label matching when available. This is robust to
            # source-order drift between desktop app versions and workbook row
            # order. If labels are unavailable, preserve positional fallback.
            for source_label in match_labels:
                for alias in _classes_jobs_label_aliases(source_label):
                    norm = _norm_label(alias)
                    if not norm:
                        continue
                    label_hits = _dedupe_hits(classes_jobs_label_idx.get(norm))
                    if label_hits and len(label_hits) == 1:
                        hits = label_hits
                        break
                if hits:
                    break

            if not hits:
                hits = classes_jobs_idx.get(source_id)
        elif bucket_tail == "desynthesis":
            hits = desynthesis_idx.get(source_id)
        elif bucket_tail in positional_sheet_idx:
            # Positional: source index lines up 1:1, in order, with the
            # workbook sheet's rows (GC ranks, companion skills, companion rank).
            hits = positional_sheet_idx[bucket_tail].get(source_id)
        elif bucket_tail == "societal-relations":
            # Keyed by (tribe, rank); the source name token carries both.
            for source_label in match_labels:
                parsed = _parse_society_rank(source_label)
                if not parsed:
                    continue
                hits = society_idx.get(parsed)
                if hits:
                    break

        if bucket.startswith("character/blue-mage/log/"):
            hits = blue_mage_idx.get((bucket, source_id))

        if not hits and bucket.startswith("logs/hunting/"):
            for source_label in match_labels:
                parsed = _parse_hunting_source_label(source_label)
                if not parsed:
                    continue
                hits = hunting_idx.get(parsed)
                if hits:
                    break

        if not hits and bucket.startswith("travel/shared-fate/"):
            for source_label in match_labels:
                parsed = _parse_place_rank(source_label)
                if not parsed:
                    continue
                zone_key, rank_value = parsed
                hits = shared_fate_idx.get((zone_key, rank_value))
                if hits:
                    break

        if not hits and bucket.startswith("travel/aether-currents/"):
            zone_key = _aether_zone_from_path(source_path_parts)
            if zone_key:
                for source_label in match_labels:
                    current_value = _parse_current_index(source_label)
                    if current_value is None:
                        continue
                    hits = aether_current_idx.get((zone_key, current_value))
                    if hits:
                        break

        # User "custom" entries have no top-level section. They are freeform and
        # frequently hold content the desktop app's game version predates, so we
        # match them conservatively: literal name only (no generic alias
        # splitting like "Gok Golma - Friendly" -> "Friendly"), no fuzzy, and
        # only when the name resolves to exactly one workbook row. Otherwise a
        # bare rank name would mark every group's row of that rank complete.
        candidate_section = _completion_top_section(source_path_parts)
        is_sectionless = candidate_section is None

        aliases: list[str] = []
        if not hits and bucket_tail not in _EXCLUSIVE_MATCH_BUCKET_TAILS:
            seen_aliases: set[str] = set()
            for source_label in match_labels:
                for base_alias in _candidate_aliases(bucket, source_label):
                    if is_sectionless:
                        ordered_aliases = [base_alias]
                    else:
                        expanded = _generic_label_aliases(base_alias)
                        ordered_aliases = [base_alias] + sorted(
                            alias for alias in expanded if alias != base_alias
                        )
                    for alias in ordered_aliases:
                        key = alias.casefold()
                        if key in seen_aliases:
                            continue
                        seen_aliases.add(key)
                        aliases.append(alias)

            bucket_exact: dict[str, list[tuple[str, int, str]]] = {}
            bucket_norm: dict[str, list[tuple[str, int, str]]] = {}
            bucket_norm_keys: list[str] = []
            for bucket_key in _bucket_lookup_chain(bucket):
                candidate_exact = exact_idx.get(bucket_key)
                candidate_norm = norm_idx.get(bucket_key)
                if candidate_exact or candidate_norm:
                    bucket_exact = candidate_exact or {}
                    bucket_norm = candidate_norm or {}
                    bucket_norm_keys = norm_keys_idx.get(bucket_key, [])
                    break

            for alias in aliases:
                hits = bucket_exact.get(alias.casefold())
                if hits:
                    break
                norm = _norm_label(alias)
                hits = bucket_norm.get(norm)
                if hits:
                    break

            if not hits and not is_sectionless:
                hits = _partial_match_hits(
                    bucket=bucket,
                    aliases=aliases,
                    bucket_norm=bucket_norm,
                    bucket_norm_keys=bucket_norm_keys,
                )

            # Cross-sheet (global) fallback is scoped to the source item's own
            # top-level section so a generic name cannot mark a row in another
            # section complete. Sections are shared 1:1 between the desktop app
            # and this workbook (character/duty/logs/travel/social). Section-less
            # (custom) items consult the unscoped index but are uniqueness-gated
            # below.
            if is_sectionless:
                section_global_exact = global_exact_idx
                section_global_norm = global_norm_idx
            else:
                section_global_exact = global_exact_by_section.get(candidate_section, {})
                section_global_norm = global_norm_by_section.get(candidate_section, {})

            if not hits:
                for alias in aliases:
                    hits = section_global_exact.get(alias.casefold())
                    if hits:
                        break
                    norm = _norm_label(alias)
                    hits = section_global_norm.get(norm)
                    if hits:
                        break

            if not hits and not is_sectionless:
                if bucket == "quest":
                    quest_aliases = [
                        alias for alias in aliases if " " in _norm_label(alias)
                    ]
                    quest_section_norm = {
                        key: val
                        for key, val in section_global_norm.items()
                        if " " in key
                    }
                    hits = _partial_match_hits_generic(
                        quest_aliases,
                        quest_section_norm,
                        cutoff=0.95,
                    )
                else:
                    hits = _partial_match_hits_generic(
                        aliases,
                        section_global_norm,
                        cutoff=0.92,
                    )

            # Custom items must map to a single unambiguous row.
            if is_sectionless:
                deduped_sectionless = _dedupe_hits(hits) or []
                if deduped_sectionless and len(deduped_sectionless) != 1:
                    hits = None

        hits = _dedupe_hits(_filter_hits_for_bucket(bucket, hits))
        hits = _dedupe_hits(
            _filter_island_sanctuary_hits(
                bucket=bucket,
                hits=hits,
                source_state=source_state,
                source_value=source_value,
            )
        )
        hits = _dedupe_hits(
            _filter_adventure_plate_hits_by_sections(
                bucket=bucket,
                source_labels=labels,
                hits=hits,
                row_sections=adventure_plate_row_sections,
            )
        )
        hits = _dedupe_hits(
            _filter_fate_hits(
                bucket=bucket,
                hits=hits,
            )
        )
        hits = _dedupe_hits(
            _filter_gathering_log_hits_by_type(
                bucket=bucket,
                hits=hits,
                row_context=row_context,
            )
        )
        hits = _dedupe_hits(
            _filter_crafting_log_hits(
                bucket=bucket,
                match_labels=match_labels,
                hits=hits,
                row_context=row_context,
            )
        )
        hits = _dedupe_hits(
            _filter_quest_hits_by_source_tokens(
                bucket=bucket,
                source_labels=labels,
                hits=hits,
                row_context=row_context,
                starting_class=effective_starting_class,
            )
        )
        hits = _dedupe_hits(
            _filter_hits_by_unlock_field(
                bucket=bucket,
                match_labels=match_labels,
                hits=hits,
                row_context=row_context,
            )
        )
        hits = _dedupe_hits(
            _select_progression_hit(
                bucket=bucket,
                match_labels=match_labels,
                hits=hits,
            )
        )
        hits = _dedupe_hits(
            _collapse_duplicate_signature_hits(
                bucket=bucket,
                hits=hits,
                row_context=row_context,
            )
        )

        if not hits:
            hits = _dedupe_hits(
                _remap_crafting_log_cross_bucket_hits(
                    bucket=bucket,
                    aliases=aliases,
                    match_labels=match_labels,
                    exact_idx=exact_idx,
                    norm_idx=norm_idx,
                    row_context=row_context,
                )
            )

        if hits and len(hits) > 1 and not _allows_multi_hit_candidate(
            bucket=bucket,
            source_labels=labels,
            match_labels=match_labels,
            hits=hits,
            row_context=row_context,
        ):
            ambiguous_candidates += 1
            primary_label = match_labels[0] if match_labels else f"id:{candidate.get('source_id')}"
            unmatched_item = {
                "bucket": bucket,
                "label": primary_label,
                "source_id": candidate.get("source_id"),
                "source_state": source_state,
                "reason": "ambiguous_multi_hit",
                "attempted_aliases": aliases[:6],
                "hit_count": len(hits),
                "hit_sheets": sorted({str(entry[0]) for entry in hits})[:8],
            }
            unmatched_items.append(unmatched_item)
            continue

        if not hits:
            if _is_ignored_untracked_candidate(bucket, candidate.get("source_id")):
                ignored_untracked += 1
                continue
            primary_label = match_labels[0] if match_labels else f"id:{candidate.get('source_id')}"
            reason, reason_extra = _unmatched_reason(
                bucket=bucket,
                raw_label=primary_label,
                aliases=aliases,
                exact_idx=exact_idx,
                norm_idx=norm_idx,
            )
            unmatched_item = {
                "bucket": bucket,
                "label": primary_label,
                "source_id": candidate.get("source_id"),
                "source_state": source_state,
                "reason": reason,
                "attempted_aliases": aliases[:6],
            }
            unmatched_item.update(reason_extra)
            unmatched_items.append(unmatched_item)
            continue

        matched_candidates += 1
        apply_state = source_state
        apply_value = source_value
        if bucket == "duty/island-sanctuary/buildings" and source_state == "value" and source_value:
            # Building completion values represent completed-slot count.
            apply_state = "done"
            apply_value = None
        for sheet_name, row_index, row_type in hits:
            _merge_row_action(
                row_actions,
                (sheet_name, row_index),
                row_type=row_type,
                state=apply_state,
                value=apply_value,
            )

    log(
        f"Matched {matched_candidates}/{total_candidates} candidates; "
        f"resolved to {len(row_actions)} workbook rows"
    )
    if ambiguous_candidates:
        log(
            f"Skipped {ambiguous_candidates} ambiguous candidates "
            "(multiple row hits after bucket filtering)"
        )

    rows_applied = 0
    rows_skipped = 0
    starting_class = effective_starting_class

    with progress_io.batch(conn, character_id):
        ordered_targets = sorted(row_actions.items(), key=lambda item: (item[0][0], item[0][1]))
        for idx, ((sheet_name, row_index), action) in enumerate(ordered_targets, start=1):
            row_type = str(action.get("row_type") or "checkbox")
            state = str(action.get("state") or "done")
            raw_value = action.get("value")
            value = float(raw_value) if isinstance(raw_value, (int, float)) else None

            current = db.effective_state(
                conn,
                character_id,
                run_id,
                sheet_name,
                row_index,
                starting_class,
            )

            if state == "done":
                if current == "done":
                    rows_skipped += 1
                    continue
                if row_type == "value":
                    target_value = value
                    if target_value is None:
                        target_value = db.value_row_cap(conn, run_id, sheet_name, row_index)
                    db.set_row_value(
                        conn,
                        character_id,
                        run_id,
                        sheet_name,
                        row_index,
                        target_value,
                        commit=False,
                        starting_class=starting_class,
                    )
                else:
                    db.set_row_state(
                        conn,
                        character_id,
                        run_id,
                        sheet_name,
                        row_index,
                        "done",
                        commit=False,
                        starting_class=starting_class,
                    )
                rows_applied += 1
                continue

            if state == "excluded":
                if current == "excluded":
                    rows_skipped += 1
                    continue
                db.set_row_state(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    "excluded",
                    commit=False,
                    starting_class=starting_class,
                )
                rows_applied += 1
                continue

            # Numeric values map to value rows directly; checkbox rows only
            # become done when the source value reaches 100%.
            if row_type == "value":
                target_value = value if value is not None else 0.0
                cap = db.value_row_cap(conn, run_id, sheet_name, row_index)
                desired_state = "done" if target_value >= cap else "todo"
                if desired_state == "done" and current == "done":
                    rows_skipped += 1
                    continue
                db.set_row_value(
                    conn,
                    character_id,
                    run_id,
                    sheet_name,
                    row_index,
                    target_value,
                    commit=False,
                    starting_class=starting_class,
                )
                rows_applied += 1
            else:
                target_value = value if value is not None else 0.0
                if target_value >= 100.0:
                    if current == "done":
                        rows_skipped += 1
                        continue
                    db.set_row_state(
                        conn,
                        character_id,
                        run_id,
                        sheet_name,
                        row_index,
                        "done",
                        commit=False,
                        starting_class=starting_class,
                    )
                    rows_applied += 1
                else:
                    rows_skipped += 1

            if idx % 60 == 0:
                log(f"Applied {idx}/{len(ordered_targets)} mapped rows")

    conn.commit()

    unmatched_candidates = len(unmatched_items)
    log(
        f"Desktop import complete: applied={rows_applied}, already_set={rows_skipped}, "
        f"unmatched_candidates={unmatched_candidates}"
    )
    if unmatched_items:
        reason_counts = Counter(str(item.get("reason") or "unknown") for item in unmatched_items)
        reason_msg = ", ".join(
            f"{reason}={count}" for reason, count in sorted(reason_counts.items())
        )
        log(f"Unmatched reasons: {reason_msg}")
    if ignored_untracked:
        log(f"Skipped {ignored_untracked} known-untracked desktop entries")

    return ImportSummary(
        character_id=character_id,
        character_name=character["name"],
        source_path=str(completion_path),
        run_id=run_id,
        total_candidates=total_candidates,
        matched_candidates=matched_candidates,
        unmatched_candidates=unmatched_candidates,
        rows_applied=rows_applied,
        rows_skipped_already_done=rows_skipped,
        unmatched_items=unmatched_items,
    )
