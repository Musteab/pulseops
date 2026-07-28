"""Static reference data for the synthetic restaurant.

These are the natural keys behind dim_outlet and dim_menu_item. They get
written out as CSV seeds so the warehouse has something to join against, which
is what makes referential-integrity checks meaningful: an event pointing at a
menu_item_id that is not in this list is a genuine orphan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Outlet:
    outlet_id: str
    name: str
    city: str
    state: str
    opened_on: str
    seats: int


@dataclass(frozen=True)
class MenuItem:
    menu_item_id: str
    name: str
    category: str
    unit_price_myr: float
    is_vegetarian: bool


OUTLETS: tuple[Outlet, ...] = (
    Outlet(
        "OUT-KL-001", "TableTap Bukit Bintang", "Kuala Lumpur",
        "WP Kuala Lumpur", "2023-03-14", 68,
    ),
    Outlet("OUT-KL-002", "TableTap Bangsar", "Kuala Lumpur", "WP Kuala Lumpur", "2023-09-01", 44),
    Outlet("OUT-SL-001", "TableTap Subang Jaya", "Subang Jaya", "Selangor", "2024-01-20", 52),
    Outlet("OUT-SL-002", "TableTap Petaling Jaya", "Petaling Jaya", "Selangor", "2024-06-11", 60),
    Outlet("OUT-PG-001", "TableTap Georgetown", "Georgetown", "Pulau Pinang", "2025-02-03", 38),
)

MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("MI-1001", "Nasi Lemak Ayam Rendang", "main", 14.90, False),
    MenuItem("MI-1002", "Char Kuey Teow", "main", 13.50, False),
    MenuItem("MI-1003", "Mee Goreng Mamak", "main", 11.00, True),
    MenuItem("MI-1004", "Nasi Goreng Kampung", "main", 12.50, True),
    MenuItem("MI-1005", "Roti Canai Set", "main", 8.00, True),
    MenuItem("MI-1006", "Laksa Penang", "main", 15.00, False),
    MenuItem("MI-1007", "Ayam Percik", "main", 18.90, False),
    MenuItem("MI-1008", "Beef Rendang Rice", "main", 19.50, False),
    MenuItem("MI-2001", "Satay Ayam (6 pcs)", "side", 12.00, False),
    MenuItem("MI-2002", "Keropok Lekor", "side", 7.50, True),
    MenuItem("MI-2003", "Acar Timun", "side", 4.50, True),
    MenuItem("MI-2004", "Telur Mata", "side", 3.00, True),
    MenuItem("MI-3001", "Teh Tarik", "drink", 4.50, True),
    MenuItem("MI-3002", "Kopi O", "drink", 3.80, True),
    MenuItem("MI-3003", "Sirap Bandung", "drink", 5.50, True),
    MenuItem("MI-3004", "Limau Ais", "drink", 4.00, True),
    MenuItem("MI-3005", "Milo Dinosaur", "drink", 8.50, True),
    MenuItem("MI-4001", "Cendol", "dessert", 7.90, True),
    MenuItem("MI-4002", "Ais Kacang", "dessert", 8.90, True),
    MenuItem("MI-4003", "Kuih Platter", "dessert", 9.50, True),
)

MENU_BY_ID: dict[str, MenuItem] = {item.menu_item_id: item for item in MENU_ITEMS}
OUTLET_IDS: tuple[str, ...] = tuple(o.outlet_id for o in OUTLETS)

# A menu id that deliberately does not exist, used by the orphan fault.
UNKNOWN_MENU_ITEM_ID = "MI-9999"


def outlets_as_rows() -> list[dict]:
    return [asdict(o) for o in OUTLETS]


def menu_items_as_rows() -> list[dict]:
    return [asdict(m) for m in MENU_ITEMS]
