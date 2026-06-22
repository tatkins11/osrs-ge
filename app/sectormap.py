"""Wiki-category-driven item -> sector classification.

The brittle name-regex classifier (see sectors.py history) misfired badly: 42% of
liquid items unclassified, charged jewellery swept into Herbs by a `(4)` rule, and one
grab-bag sector holding 60% of gp-volume. This module instead classifies from the OSRS
Wiki's OWN categories (equipment slot, combat style, skill, drop source), which are far
more reliable.

Firewall note: the wiki is only reachable from the VPS, so the MAP (name -> sector) is
generated there and cached in DATA_DIR/item_sectors.json (read by the API, refreshed by
the collector). The RULES live here in code; the RESULT lives in the data volume.
"""
from __future__ import annotations

import json
import logging
import re
import ssl

import httpx
import truststore

from .config import DATA_DIR, USER_AGENT

log = logging.getLogger("sectormap")
WIKI_API = "https://oldschool.runescape.wiki/api.php"
MAP_PATH = DATA_DIR / "item_sectors.json"

# --- wiki category fetch ----------------------------------------------------
_VARIANT_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")  # strip trailing "(4)", "(uncharged)", "(or)" ... -> base article


def _base_title(name: str) -> str:
    return _VARIANT_SUFFIX.sub("", name).strip()


def fetch_categories(names: list[str]) -> dict[str, set[str]]:
    """name(lower) -> set of (non-hidden) wiki category names, via base-article lookup."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # map each base title -> the original names that reduced to it
    title_to_names: dict[str, list[str]] = {}
    for n in names:
        title_to_names.setdefault(_base_title(n), []).append(n)
    titles = list(title_to_names)
    title_cats: dict[str, set[str]] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, verify=ctx) as c:
        for i in range(0, len(titles), 20):  # small batches keep categories under cllimit
            batch = titles[i:i + 20]
            cont: dict = {}
            while True:
                j = c.get(WIKI_API, params={
                    "action": "query", "format": "json", "titles": "|".join(batch),
                    "prop": "categories", "cllimit": "500", "clshow": "!hidden", "redirects": "1", **cont,
                }).json()
                q = j.get("query", {})
                # resolve normalized + redirect chains so we can map results back to our titles
                remap = {}
                for r in q.get("normalized", []) + q.get("redirects", []):
                    remap[r["to"]] = r["from"]

                def origin(t: str) -> str:
                    while t in remap:
                        t = remap[t]
                    return t
                for pg in q.get("pages", {}).values():
                    if "missing" in pg:
                        continue
                    src = origin(pg["title"])
                    title_cats.setdefault(src, set()).update(
                        x["title"].replace("Category:", "") for x in pg.get("categories", [])
                    )
                cont = j.get("continue", {})
                if not cont:
                    break
    out: dict[str, set[str]] = {}
    for title, orig_names in title_to_names.items():
        cats = title_cats.get(title, set())
        for n in orig_names:
            out[n.lower()] = cats
    return out


# --- category -> sector rules -----------------------------------------------
# Sector keys (the approved ~21). Ordered, first matching rule wins.
def _has(cats: set[str], *needles: str) -> bool:
    return any(n in cats for n in needles)


# exact-name overrides for items the rules can't reach (no useful categories)
OVERRIDES = {
    "old school bond": "treasure", "team-33 cape": "treasure",
    "saturated heart": "magic_gear", "imbued heart": "magic_gear", "kodai insignia": "magic_gear",
    "avernic treads": "melee_armour", "avernic defender hilt": "melee_weapons",
    "dexterous prayer scroll": "boss_components", "arcane prayer scroll": "boss_components",
}
# armour/robe SET bundle items (no equip categories) -> style by name keyword
_SET_STYLE = [
    (("ancestral", "virtus", "ahrim", "dagon", "blue moon", "skeletal"), "magic_gear"),
    (("masori", "armadyl", "karil", "eclipse moon", "crystal", "void", "d'hide", "dragonhide", "blessed"), "ranged_gear"),
    (("torva", "justiciar", "inquisitor", "bandos", "dharok", "guthan", "torag", "verac",
      "obsidian", "oathplate", "blood moon", "gorilla", "barrows"), "melee_armour"),
]
_RAID_SOURCE = ("Chambers of Xeric", "Theatre of Blood", "Tombs of Amascut", "The Nightmare",
                "Nex", "Phantom Muspah", "Desert Treasure II", "God Wars Dungeon")


def sector_of(name: str, cats: set[str]) -> str | None:
    """Classify one item from its lower-cased name + its wiki category set."""
    n = name.lower()
    if n in OVERRIDES:
        return OVERRIDES[n]
    # armour/robe set bundles (e.g. "Torva armour set", "Ancestral robes set")
    if re.search(r"\bset$|armour set|robes? set", n):
        for kws, sec in _SET_STYLE:
            if any(k in n for k in kws):
                return sec
        return "melee_armour"  # most generic armour sets are melee

    # 1) cosmetics / treasure first (so a 3rd age platebody isn't filed as melee armour)
    if _has(cats, "Treasure Trails rewards", "Master clue rewards", "Holiday items",
            "Discontinued items", "Ornament kits", "Third age equipment") \
            or re.search(r"3rd age|\bgilded\b|ornament kit|partyhat|santa hat|h'?ween|halloween mask|\bsled\b", n):
        return "treasure"

    # 2) jewellery (catches charged jewellery the old (4) rule stole into Herbs)
    if _has(cats, "Jewellery", "Amulets", "Rings", "Necklaces", "Bracelets") \
            or re.search(r"\b(amulet|necklace|bracelet|tiara)\b|\bring\b(?! of recoil)", n):
        return "jewellery"

    # 3) ammunition (finished projectiles) -- before ranged gear / fletching
    if re.search(r"\b(arrows?|bolts?|darts?|javelins?|knives|cannonball|chinchompas?)\b", n) \
            and not re.search(r"tips?\b|shafts?\b|\(unf\)$|bolt of cloth|unstrung", n):
        return "ammo"

    # 4) combat gear by style (categories are reliable here; names were not)
    if _has(cats, "Magic weapons", "Magic armour", "Staves", "Battlestaves") \
            or re.search(r"\b(staff|staves|wand|trident|tome|mystic|ward|battlestaff)\b", n):
        return "magic_gear"
    if _has(cats, "Ranged weapons", "Ranged armour", "Bows", "Crossbows"):
        return "ranged_gear"
    if _has(cats, "Melee weapons", "Crush weapons", "Slash weapons", "Stab weapons", "Spears", "Two-handed slot items") \
            and not _has(cats, "Magic weapons", "Ranged weapons"):
        return "melee_weapons"
    if _has(cats, "Melee armour"):
        return "melee_armour"

    # 5) skilling resources (names are reliable for raw materials; categories fill gaps)
    if re.search(r"\blogs?$", n) or _has(cats, "Logs"):
        return "logs"
    if re.search(r"\b(ore|bar)s?$|\bcoal$", n) or _has(cats, "Ores", "Bars"):
        return "ores_bars"
    if _has(cats, "Gems") or re.search(r"^(uncut )?(sapphire|emerald|ruby|diamond|dragonstone|onyx|zenyte|opal|jade|red topaz)", n):
        return "gems"
    if re.search(r"(seed|sapling|spore)s?$", n) or _has(cats, "Seeds"):
        return "seeds"
    if re.search(r"\b(bones|ashes)$|\bensouled\b", n) or _has(cats, "Prayer"):
        return "bones_prayer"
    if re.search(r"\(\d\)$|\bpotion|\bbrew|\bmix\b", n) and _has(cats, "Herblore"):
        return "potions"
    if _has(cats, "Herblore") or re.search(r"\b(grimy|clean)\b|herb\b", n):
        return "herbs"
    if re.search(r"\b(rune|teleport)\b|\(tablet\)$", n) and not _has(cats, "Melee weapons", "Ranged weapons", "Magic weapons"):
        return "runes"
    if _has(cats, "Fishing", "Cooking", "Food and Drink") or re.search(r"^raw |^cooked |\b(shark|lobster|tuna|swordfish|monkfish|karambwan|anglerfish|manta ray|sea turtle)\b", n):
        return "food_fishing"
    if re.search(r"\b(arrow|dart|javelin|bolt) ?tips?\b|shafts?\b|bow string\b|unstrung|\(unf\)$|headless arrow|\bfeather", n) or _has(cats, "Fletching"):
        return "fletching_mats"
    if _has(cats, "Construction") or re.search(r"\bplanks?\b|marble block|limestone brick|gold leaf", n):
        return "construction"
    if re.search(r"zulrah's scales|revenant ether|\bsplinters\b|ancient essence|aether catalyst|\bblighted\b|\bnumulite\b|bottled (storm|dread|mind)", n) or _has(cats, "Items with charges"):
        return "charges"

    # 6) boss-drop NON-gear uniques (components for BiS gear): dropped by a monster or
    #    a raid, not equipable, and not caught by any resource rule above.
    if (_has(cats, "Items dropped by monster") or any(r in cats for r in _RAID_SOURCE)) \
            and not _has(cats, "Equipable items"):
        return "boss_components"

    # 7) leftover skilling inputs
    if _has(cats, "Crafting", "Smithing", "Firemaking", "Fletching", "Runecraft", "Farming"):
        return "misc_skilling"
    return None


# --- build + load -----------------------------------------------------------
def build_and_save(items: list[dict]) -> dict[str, str]:
    """items: [{item_id, name}]. Fetch categories, classify, persist name(lower)->sector."""
    names = [it["name"] for it in items if it.get("name")]
    cats = fetch_categories(names)
    mapping = {}
    for nm in names:
        s = sector_of(nm, cats.get(nm.lower(), set()))
        if s:
            mapping[nm.lower()] = s
    MAP_PATH.write_text(json.dumps(mapping))
    log.info("sector map built: %d/%d classified -> %s", len(mapping), len(names), MAP_PATH)
    return mapping


_CACHE: tuple[float, dict] = (-1.0, {})


def load_map() -> dict[str, str]:
    """Cached name(lower)->sector map, reloaded when the file changes (so a collector
    refresh is picked up without an API restart)."""
    global _CACHE
    try:
        mt = MAP_PATH.stat().st_mtime
    except OSError:
        return {}
    if mt != _CACHE[0]:
        try:
            _CACHE = (mt, json.loads(MAP_PATH.read_text()))
        except Exception:
            _CACHE = (mt, {})
    return _CACHE[1]
