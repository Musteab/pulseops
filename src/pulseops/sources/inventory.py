"""daily inventory snapshots, the batch source.

a snapshot table rather than a movement log: one row per outlet per item per
day, recording what was on hand at close. that grain is chosen deliberately.
stock movements would let you reconstruct any point in time but make "what did
we have on Tuesday" an expensive question, and the dashboards only ever ask the
cheap version.

deterministic from a seed, like everything else here, so a run on any machine
on any date produces the same file.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from ..generator.catalog import MENU_ITEMS, OUTLETS

# how many days of stock a busy outlet carries. drinks turn over faster than
# mains, and desserts sit around.
_BASE_STOCK = {"main": 45, "side": 60, "drink": 120, "dessert": 25}

# outlets carry stock roughly in proportion to how busy they are
_OUTLET_SCALE = {
    "OUT-KL-001": 1.35,
    "OUT-KL-002": 0.80,
    "OUT-SL-001": 0.95,
    "OUT-SL-002": 0.90,
    "OUT-PG-001": 0.60,
}


def generate_inventory(
    snapshot_date: date | None = None,
    days: int = 1,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """one row per outlet per menu item per day."""
    if days < 1:
        raise ValueError("days must be at least 1")

    end = snapshot_date or date.today()
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []

    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        for outlet in OUTLETS:
            scale = _OUTLET_SCALE.get(outlet.outlet_id, 1.0)
            for item in MENU_ITEMS:
                base = _BASE_STOCK[item.category] * scale
                on_hand = max(0, int(rng.gauss(base, base * 0.28)))

                # a few lines run out on any given day, which is what makes
                # "did we lose sales to stockouts" a question worth asking
                is_stockout = on_hand == 0 or rng.random() < 0.03
                if is_stockout:
                    on_hand = 0

                rows.append(
                    {
                        "snapshot_date": day.isoformat(),
                        "outlet_id": outlet.outlet_id,
                        "menu_item_id": item.menu_item_id,
                        "units_on_hand": on_hand,
                        "is_stockout": on_hand == 0,
                        "unit_cost_myr": round(item.unit_price_myr * 0.34, 2),
                    }
                )

    return rows


def validate_inventory_rows(rows: list[dict[str, Any]]) -> list[str]:
    """the batch equivalent of the streaming contract.

    deliberately lighter than contracts.py: this is a file we control, arriving
    once a day, so the failure modes are a truncated export or a duplicated
    load rather than a producer quietly renaming a field.
    """
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for row in rows:
        key = (
            str(row.get("snapshot_date")),
            str(row.get("outlet_id")),
            str(row.get("menu_item_id")),
        )
        if any(part in ("None", "") for part in key):
            problems.append(f"row missing part of its key: {row}")
            continue
        if key in seen:
            problems.append(f"duplicate snapshot row for {key}")
        seen.add(key)

        units = row.get("units_on_hand")
        if not isinstance(units, int) or units < 0:
            problems.append(f"{key}: units_on_hand {units} is not a non-negative integer")

        if row.get("is_stockout") is not (units == 0):
            problems.append(f"{key}: is_stockout disagrees with units_on_hand {units}")

    return problems


def to_csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """the shape a daily csv drop would actually have."""
    return [
        {
            "snapshot_date": r["snapshot_date"],
            "outlet_id": r["outlet_id"],
            "menu_item_id": r["menu_item_id"],
            "units_on_hand": r["units_on_hand"],
            "unit_cost_myr": r["unit_cost_myr"],
        }
        for r in rows
    ]
