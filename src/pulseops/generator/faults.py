"""Deliberate data-quality faults injected into the synthetic stream.

Every fault is recorded in a manifest with the event id it was applied to, so
the pipeline's detection rate can be measured against ground truth instead of
guessed at. This is the difference between "I built quality checks" and "my
quality checks caught 47 of 50 injected faults, and here is the one class they
missed".

Faults are tagged with the layer expected to catch them:

  contract   the ingest-time contract validator should reject the record
  warehouse  only visible with reference data or across records, so it is
             caught by a dbt test or a quality query, not at ingest
"""

from __future__ import annotations

import copy
import random
from collections.abc import Callable
from dataclasses import dataclass

from .catalog import UNKNOWN_MENU_ITEM_ID

Event = dict


@dataclass(frozen=True)
class FaultRecord:
    """Ground truth for one injected fault."""

    event_id: str
    fault_type: str
    detected_by: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "fault_type": self.fault_type,
            "detected_by": self.detected_by,
            "path": self.path,
            "detail": self.detail,
        }


# Each injector takes a valid event plus an rng and returns the events to emit
# in its place, along with the ground-truth record.
Injector = Callable[[Event, random.Random], tuple[list[Event], FaultRecord]]

_REGISTRY: dict[str, tuple[str, Injector]] = {}


def fault(name: str, detected_by: str):
    def wrap(fn: Injector) -> Injector:
        _REGISTRY[name] = (detected_by, fn)
        return fn

    return wrap


@fault("schema_drift", "contract")
def _schema_drift(event: Event, rng: random.Random):
    """Upstream renames the total field and bumps its version without telling us.

    This is the fault behind the headline demo: revenue appears to collapse for
    one outlet, and the question is whether sales dropped or the pipeline broke.
    """
    bad = copy.deepcopy(event)
    bad["total_amount"] = bad.pop("order_total_myr")
    bad["schema_version"] = "2.0.0"
    return [bad], FaultRecord(
        event["event_id"],
        "schema_drift",
        "contract",
        "order_total_myr",
        "renamed to total_amount, schema_version bumped to 2.0.0",
    )


@fault("duplicate_event", "warehouse")
def _duplicate_event(event: Event, rng: random.Random):
    """At-least-once delivery replays the same event id."""
    twin = copy.deepcopy(event)
    return [event, twin], FaultRecord(
        event["event_id"],
        "duplicate_event",
        "warehouse",
        "event_id",
        "same event_id emitted twice",
    )


@fault("negative_unit_price", "contract")
def _negative_unit_price(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    idx = rng.randrange(len(bad["lines"]))
    bad["lines"][idx]["unit_price_myr"] = -abs(bad["lines"][idx]["unit_price_myr"])
    return [bad], FaultRecord(
        event["event_id"],
        "negative_unit_price",
        "contract",
        f"lines[{idx}].unit_price_myr",
        "unit price flipped negative",
    )


@fault("missing_outlet_id", "contract")
def _missing_outlet_id(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    bad.pop("outlet_id", None)
    return [bad], FaultRecord(
        event["event_id"], "missing_outlet_id", "contract", "outlet_id", "field dropped"
    )


@fault("null_order_total", "contract")
def _null_order_total(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    bad["order_total_myr"] = None
    return [bad], FaultRecord(
        event["event_id"], "null_order_total", "contract", "order_total_myr", "set to null"
    )


@fault("orphan_menu_item", "warehouse")
def _orphan_menu_item(event: Event, rng: random.Random):
    """Points at a menu item that does not exist in the dimension."""
    bad = copy.deepcopy(event)
    idx = rng.randrange(len(bad["lines"]))
    bad["lines"][idx]["menu_item_id"] = UNKNOWN_MENU_ITEM_ID
    return [bad], FaultRecord(
        event["event_id"],
        "orphan_menu_item",
        "warehouse",
        f"lines[{idx}].menu_item_id",
        f"references {UNKNOWN_MENU_ITEM_ID}, absent from dim_menu_item",
    )


@fault("unparseable_timestamp", "contract")
def _unparseable_timestamp(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    bad["event_ts"] = "29/07/2026 10:15 AM"
    return [bad], FaultRecord(
        event["event_id"],
        "unparseable_timestamp",
        "contract",
        "event_ts",
        "emitted as DD/MM/YYYY instead of ISO-8601",
    )


@fault("line_total_mismatch", "contract")
def _line_total_mismatch(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    idx = rng.randrange(len(bad["lines"]))
    bad["lines"][idx]["line_total_myr"] = round(
        bad["lines"][idx]["line_total_myr"] + rng.uniform(1.0, 9.0), 2
    )
    return [bad], FaultRecord(
        event["event_id"],
        "line_total_mismatch",
        "contract",
        f"lines[{idx}].line_total_myr",
        "line total no longer equals qty * unit_price",
    )


@fault("unknown_channel", "contract")
def _unknown_channel(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    bad["channel"] = "kiosk"
    return [bad], FaultRecord(
        event["event_id"],
        "unknown_channel",
        "contract",
        "channel",
        "new enum value 'kiosk' not in the agreed set",
    )


@fault("empty_lines", "contract")
def _empty_lines(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    bad["lines"] = []
    bad["order_total_myr"] = 0.0
    return [bad], FaultRecord(
        event["event_id"], "empty_lines", "contract", "lines", "order has no line items"
    )


@fault("late_arrival", "warehouse")
def _late_arrival(event: Event, rng: random.Random):
    """Valid record, but it shows up days after the fact and lands in a closed window."""
    bad = copy.deepcopy(event)
    bad["_late_by_days"] = rng.randint(2, 9)
    return [bad], FaultRecord(
        event["event_id"],
        "late_arrival",
        "warehouse",
        "event_ts",
        f"event_ts is {bad['_late_by_days']} days behind ingest_ts",
    )


@fault("negative_qty", "contract")
def _negative_qty(event: Event, rng: random.Random):
    bad = copy.deepcopy(event)
    idx = rng.randrange(len(bad["lines"]))
    bad["lines"][idx]["qty"] = -bad["lines"][idx]["qty"]
    return [bad], FaultRecord(
        event["event_id"],
        "negative_qty",
        "contract",
        f"lines[{idx}].qty",
        "quantity flipped negative",
    )


FAULT_TYPES: tuple[str, ...] = tuple(sorted(_REGISTRY))


def detected_by(fault_type: str) -> str:
    return _REGISTRY[fault_type][0]


def apply_fault(
    fault_type: str, event: Event, rng: random.Random
) -> tuple[list[Event], FaultRecord]:
    if fault_type not in _REGISTRY:
        raise KeyError(f"unknown fault type {fault_type!r}, known: {FAULT_TYPES}")
    return _REGISTRY[fault_type][1](event, rng)
