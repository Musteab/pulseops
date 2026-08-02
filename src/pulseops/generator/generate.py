"""Deterministic synthetic order-event generator.

Same seed in, byte-identical events out. That matters more than it sounds:
every quality number in this repo is only credible if the run that produced it
can be reproduced, and every agent evaluation needs a fixed world to be scored
against.

Traffic is shaped rather than uniform. Outlets have different sizes, lunch and
dinner peak, weekends run heavier, and payment failures cluster on ewallet.
Flat random data makes every anomaly look the same and makes the dashboards
boring.
"""

from __future__ import annotations

import math
import random
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..contracts import CURRENT_VERSION
from .catalog import MENU_ITEMS, OUTLETS, MenuItem
from .faults import FAULT_TYPES, FaultRecord, apply_fault, detected_by

# Relative order volume per outlet. Bukit Bintang is the flagship.
_OUTLET_WEIGHTS = (0.32, 0.18, 0.20, 0.19, 0.11)

# Hour-of-day weights, midnight to 23:00. Two humps: lunch and dinner.
_HOUR_WEIGHTS = (
    0.2, 0.1, 0.1, 0.1, 0.2, 0.6, 1.5, 3.0, 4.0, 3.2, 3.5, 7.0,
    9.5, 8.0, 4.5, 3.0, 3.2, 5.0, 8.5, 9.0, 6.5, 4.0, 2.0, 0.8,
)

_CHANNEL_WEIGHTS = {"dine_in": 0.52, "takeaway": 0.28, "delivery": 0.20}
_METHOD_WEIGHTS = {"card": 0.44, "ewallet": 0.34, "cash": 0.14, "online_banking": 0.08}

# Base probability an authorisation fails, per payment method.
_FAILURE_RATE = {"card": 0.018, "ewallet": 0.041, "cash": 0.0, "online_banking": 0.026}


@dataclass
class GenerationResult:
    events: list[dict]
    faults: list[FaultRecord]
    run_id: str
    seed: int
    start: date
    end: date
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def clean_event_count(self) -> int:
        return len(self.events) - len(self.faults)

    def manifest(self) -> dict[str, Any]:
        """Ground truth for the eval suite."""
        counts: dict[str, int] = {}
        for f in self.faults:
            counts[f.fault_type] = counts.get(f.fault_type, 0) + 1

        by_layer: dict[str, int] = {}
        for f in self.faults:
            by_layer[f.detected_by] = by_layer.get(f.detected_by, 0) + 1

        return {
            "run_id": self.run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "seed": self.seed,
            "window": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "params": self.params,
            "event_count": len(self.events),
            "fault_count": len(self.faults),
            "fault_counts_by_type": dict(sorted(counts.items())),
            "fault_counts_by_layer": dict(sorted(by_layer.items())),
            "faults": [f.as_dict() for f in self.faults],
        }


def _weighted_choice(rng: random.Random, options: Iterable[str], weights: Iterable[float]) -> str:
    return rng.choices(list(options), weights=list(weights), k=1)[0]


def _pick_timestamp(rng: random.Random, start: date, end: date) -> datetime:
    span_days = (end - start).days
    day_offset = rng.randint(0, max(span_days, 0))
    day = start + timedelta(days=day_offset)

    # Weekends run about 35 percent heavier, modelled by resampling toward them.
    #
    # the roll is drawn before the weekday check rather than inside it, and that
    # ordering is the whole point. written as `day.weekday() < 5 and rng.random()`
    # python short-circuits, so no random number is drawn on a weekend, so the
    # number of draws depends on which days the window happens to contain. same
    # seed, different month, different data. it made `make demo` report 185
    # faults one day and 193 the next, which quietly broke the one promise this
    # whole project rests on.
    shift_toward_weekend = rng.random() < 0.12
    if day.weekday() < 5 and shift_toward_weekend:
        day += timedelta(days=(5 - day.weekday()))
        if day > end:
            day = end

    hour = int(_weighted_choice(rng, range(24), _HOUR_WEIGHTS))
    return datetime(
        day.year, day.month, day.day,
        hour, rng.randrange(60), rng.randrange(60),
        rng.randrange(0, 1_000_000),
        tzinfo=UTC,
    )


def _basket(rng: random.Random) -> list[MenuItem]:
    """Pick a plausible set of items rather than uniform noise."""
    mains = [m for m in MENU_ITEMS if m.category == "main"]
    sides = [m for m in MENU_ITEMS if m.category == "side"]
    drinks = [m for m in MENU_ITEMS if m.category == "drink"]
    desserts = [m for m in MENU_ITEMS if m.category == "dessert"]

    party = rng.choices((1, 2, 3, 4, 5), weights=(0.34, 0.31, 0.17, 0.12, 0.06))[0]
    chosen = [rng.choice(mains) for _ in range(party)]

    if rng.random() < 0.55:
        chosen.extend(rng.sample(sides, k=min(rng.randint(1, 2), len(sides))))
    if rng.random() < 0.78:
        chosen.extend(rng.choice(drinks) for _ in range(max(1, party - 1)))
    if rng.random() < 0.22:
        chosen.append(rng.choice(desserts))

    return chosen


def _build_event(rng: random.Random, start: date, end: date) -> dict:
    outlet = _weighted_choice(rng, (o.outlet_id for o in OUTLETS), _OUTLET_WEIGHTS)
    ts = _pick_timestamp(rng, start, end)
    channel = _weighted_choice(rng, _CHANNEL_WEIGHTS, _CHANNEL_WEIGHTS.values())
    method = _weighted_choice(rng, _METHOD_WEIGHTS, _METHOD_WEIGHTS.values())

    # Collapse duplicate picks into quantities so a basket of 3 teh tariks is
    # one line with qty 3, the way a real POS would record it.
    counts: dict[str, int] = {}
    for item in _basket(rng):
        counts[item.menu_item_id] = counts.get(item.menu_item_id, 0) + 1

    lines = []
    for menu_item_id, qty in counts.items():
        item = next(m for m in MENU_ITEMS if m.menu_item_id == menu_item_id)
        unit = item.unit_price_myr
        lines.append(
            {
                "menu_item_id": menu_item_id,
                "name": item.name,
                "qty": qty,
                "unit_price_myr": unit,
                "line_total_myr": round(qty * unit, 2),
            }
        )

    total = round(sum(line["line_total_myr"] for line in lines), 2)

    failed = rng.random() < _FAILURE_RATE[method]
    if failed:
        status = "failed"
    elif rng.random() < 0.006:
        status = "refunded"
    elif rng.random() < 0.004:
        status = "pending"
    else:
        status = "captured"

    return {
        "event_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "event_type": "order.placed",
        "schema_version": CURRENT_VERSION,
        "event_ts": ts.isoformat().replace("+00:00", "Z"),
        "outlet_id": outlet,
        "order_id": f"ORD-{ts:%Y%m%d}-{rng.randrange(16**6):06X}",
        "customer_id": None if rng.random() < 0.18 else f"CUS-{rng.randrange(16**8):08X}",
        "channel": channel,
        "order_total_myr": total,
        "payment": {
            "method": method,
            "status": status,
            "amount_myr": 0.0 if status == "failed" else total,
        },
        "lines": lines,
    }


def generate(
    n_events: int,
    seed: int = 42,
    start: date | None = None,
    end: date | None = None,
    fault_rate: float = 0.0,
    fault_types: tuple[str, ...] = FAULT_TYPES,
) -> GenerationResult:
    """Produce `n_events` clean events, then corrupt `fault_rate` of them.

    The returned list may be longer than `n_events` because some faults, such
    as duplicate delivery, emit an extra record.
    """
    if n_events < 1:
        raise ValueError("n_events must be at least 1")
    if not 0.0 <= fault_rate <= 1.0:
        raise ValueError("fault_rate must be between 0 and 1")
    unknown = set(fault_types) - set(FAULT_TYPES)
    if unknown:
        raise ValueError(f"unknown fault types: {sorted(unknown)}")

    end = end or date.today()
    start = start or (end - timedelta(days=29))
    if start > end:
        raise ValueError("start must not be after end")

    rng = random.Random(seed)
    run_id = f"run-{seed}-{n_events}"

    clean = [_build_event(rng, start, end) for _ in range(n_events)]

    n_faulty = math.floor(n_events * fault_rate)
    faulty_positions = set(rng.sample(range(n_events), k=n_faulty)) if n_faulty else set()

    events: list[dict] = []
    faults: list[FaultRecord] = []

    # Anchor for records whose own timestamp cannot be parsed. Deriving it from
    # the window instead of the wall clock keeps runs byte-identical.
    fallback_now = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)

    for position, event in enumerate(clean):
        if position not in faulty_positions:
            events.append(_finalise(event, rng, fallback_now))
            continue

        chosen = rng.choice(list(fault_types))
        produced, record = apply_fault(chosen, event, rng)
        events.extend(_finalise(e, rng, fallback_now) for e in produced)
        faults.append(record)

    events.sort(key=lambda e: e.get("ingest_ts", ""))

    return GenerationResult(
        events=events,
        faults=faults,
        run_id=run_id,
        seed=seed,
        start=start,
        end=end,
        params={
            "n_events": n_events,
            "fault_rate": fault_rate,
            "fault_types": list(fault_types),
        },
    )


def _read_producer_clock(raw_ts: str) -> datetime | None:
    """Recover the moment an event happened, whatever shape it was written in.

    ISO-8601 first, then the locale format the unparseable_timestamp fault
    produces. The second one matters: this stands in for the producer's own
    clock, and a till knows when it took an order even when it serialises the
    time badly. Treating a misformatted timestamp as unknown would stamp ingest
    weeks away from the event, and every such record would then look like a
    late arrival once it was repaired. That is a reporting artefact, not a
    fault, and it inflated the warehouse fault count by exactly the number of
    misformatted records.
    """
    try:
        return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(raw_ts, "%d/%m/%Y %I:%M %p").replace(tzinfo=UTC)
    except ValueError:
        return None


def _finalise(event: dict, rng: random.Random, fallback_now: datetime) -> dict:
    """Stamp ingest metadata, which is set by the platform and not the producer."""
    late_days = event.pop("_late_by_days", 0)

    # both draws happen unconditionally, for the same reason the weekend roll
    # does: a branch that skips an rng call makes the stream depend on the data,
    # and then the same seed stops producing the same dataset.
    prompt_delay = rng.uniform(0.4, 12.0)
    late_delay = rng.uniform(0.0, 3600.0)

    ingest = fallback_now
    raw_ts = event.get("event_ts")
    if isinstance(raw_ts, str):
        parsed = _read_producer_clock(raw_ts)
        if parsed is not None:
            # Normal path: ingest lands a few seconds after the event happened.
            ingest = parsed + timedelta(seconds=prompt_delay)
            if late_days:
                ingest = parsed + timedelta(days=late_days, seconds=late_delay)

    event["ingest_ts"] = ingest.isoformat().replace("+00:00", "Z")
    return event


__all__ = ["generate", "GenerationResult", "FAULT_TYPES", "detected_by"]
