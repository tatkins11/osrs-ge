"""Grand Exchange tax engine.

Verified mechanics (June 2026):
  * 2% tax on the SELL side, paid by the seller out of proceeds.
  * Rounded DOWN to the nearest gp -> anything sold for < 50 gp is untaxed.
  * Capped at 5,000,000 gp per item (binds at a sale price >= 250,000,000).
  * A fixed set of items (bonds + skilling/newbie staples) is fully exempt.
"""
from __future__ import annotations

import math

import numpy as np

from .config import TAX_CAP, TAX_MIN_PRICE, TAX_RATE

# Items fully exempt from GE tax. The canonical list is the wiki category
# "Items exempt from Grand Exchange tax"; these are all low-value, non-flip
# items so the engine treats exemption as a name-match convenience.
EXEMPT_ITEM_NAMES = frozenset(
    n.lower()
    for n in [
        "Old school bond",
        # Skilling tools
        "Chisel", "Gardening trowel", "Glassblowing pipe", "Hammer", "Needle",
        "Pestle and mortar", "Rake", "Saw", "Secateurs", "Seed dibber",
        "Shears", "Spade", "Watering can",
        # Jewellery / teleports
        "Games necklace", "Ring of dueling",
        "Varrock teleport", "Lumbridge teleport", "Falador teleport",
        "Kourend castle teleport", "Civitas illa fortis teleport",
        "Teleport to house",
        # Basic food
        "Cooked chicken", "Cooked meat", "Meat pie", "Energy potion",
        "Shrimps", "Herring", "Mackerel", "Pike", "Salmon", "Tuna", "Lobster",
        # Low-tier ammo / runes
        "Mind rune", "Iron arrow", "Steel arrow", "Iron dart", "Steel dart",
    ]
)


def is_exempt(item_name: str | None) -> bool:
    if not item_name:
        return False
    return item_name.strip().lower() in EXEMPT_ITEM_NAMES


def sell_tax(price: int, exempt: bool = False) -> int:
    """GE tax charged when selling a single item at ``price``."""
    if exempt or price < TAX_MIN_PRICE:
        return 0
    return min(int(math.floor(price * TAX_RATE)), TAX_CAP)


def net_sell(price: int, exempt: bool = False) -> int:
    """Coins actually received per item after tax."""
    return price - sell_tax(price, exempt)


def margin(buy_price: int, sell_price: int, exempt: bool = False) -> int:
    """Net profit per item: buy at ``buy_price``, sell at ``sell_price`` (after tax)."""
    return net_sell(sell_price, exempt) - buy_price


def roi(buy_price: int, sell_price: int, exempt: bool = False) -> float:
    """Return on investment as a fraction (0.01 == 1%)."""
    if buy_price <= 0:
        return 0.0
    return margin(buy_price, sell_price, exempt) / buy_price


def sell_tax_array(prices, exempt=None) -> np.ndarray:
    """Vectorised sell tax over an array of prices (for whole-market computation).

    Returns a float array (NaN-safe). ``exempt`` is an optional boolean array.
    """
    p = np.asarray(prices, dtype="float64")
    t = np.minimum(np.floor(p * TAX_RATE), TAX_CAP)
    t = np.where(p < TAX_MIN_PRICE, 0.0, t)
    if exempt is not None:
        t = np.where(np.asarray(exempt, dtype=bool), 0.0, t)
    return np.where(np.isnan(p), 0.0, t)
