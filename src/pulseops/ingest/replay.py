"""repairing quarantined records and sending them back through.

the rule this module lives by: repair format, never invent facts.

a record whose total was renamed by a producer upgrade is recoverable, because
the number is still sitting there under a different key. a record with no
outlet_id is not, because no amount of cleverness tells you which restaurant
took the order. guessing would make the dashboards green and the numbers wrong,
which is worse than leaving them red.

so every repair below is a pure renaming or reformatting of data that is
already present. anything that would require inventing a value is refused, and
the refusal is recorded with a reason rather than swallowed.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..contracts import CURRENT_VERSION, validate_event

Event = dict[str, Any]


@dataclass(frozen=True)
class Repair:
    name: str
    describes: str
    can_apply: Callable[[Event], bool]
    apply: Callable[[Event], Event]


_REPAIRS: list[Repair] = []


def repair(name: str, describes: str):
    def wrap(fn: Callable[[Event], Event]) -> Callable[[Event], Event]:
        checker = getattr(fn, "_can_apply", None)
        if checker is None:
            raise RuntimeError(f"repair {name} has no can_apply, add @applies_when first")
        _REPAIRS.append(Repair(name=name, describes=describes, can_apply=checker, apply=fn))
        return fn

    return wrap


def applies_when(predicate: Callable[[Event], bool]):
    def wrap(fn: Callable[[Event], Event]) -> Callable[[Event], Event]:
        fn._can_apply = predicate  # type: ignore[attr-defined]
        return fn

    return wrap


# ---------------------------------------------------------------------------
# the repairs
# ---------------------------------------------------------------------------


@repair(
    "v2_total_rename",
    "producer 2.0.0 renamed order_total_myr to total_amount",
)
@applies_when(
    lambda e: e.get("schema_version") == "2.0.0"
    and "total_amount" in e
    and "order_total_myr" not in e
)
def _repair_v2_total(event: Event) -> Event:
    """undo the rename that caused the revenue drop.

    this is the adapter you would genuinely write after a producer upgrade: you
    do not get to tell the vendor to roll back, so you learn to read their new
    shape and normalise it to the one your warehouse agreed to.
    """
    fixed = copy.deepcopy(event)
    fixed["order_total_myr"] = fixed.pop("total_amount")
    fixed["schema_version"] = CURRENT_VERSION
    return fixed


_DDMMYYYY = re.compile(
    r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)$", re.IGNORECASE
)


@repair(
    "locale_timestamp",
    "producer emitted DD/MM/YYYY hh:mm AM instead of ISO-8601",
)
@applies_when(lambda e: bool(_DDMMYYYY.match(str(e.get("event_ts", "")))))
def _repair_locale_timestamp(event: Event) -> Event:
    """reformat a British-style timestamp into ISO-8601.

    safe because nothing is being guessed: the day, month, year and time are all
    present, they are simply arranged in an order BigQuery will not parse. note
    the DD/MM assumption is only defensible because we know this producer is
    Malaysian. the same string from a US till would mean a different date, which
    is exactly why this repair is narrow and named after its locale.
    """
    fixed = copy.deepcopy(event)
    match = _DDMMYYYY.match(str(fixed["event_ts"]))
    assert match is not None  # guarded by can_apply

    day, month, year, hour, minute, meridiem = match.groups()
    hour24 = int(hour) % 12
    if meridiem.upper() == "PM":
        hour24 += 12

    fixed["event_ts"] = (
        datetime(int(year), int(month), int(day), hour24, int(minute), tzinfo=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return fixed


# ---------------------------------------------------------------------------
# applying them
# ---------------------------------------------------------------------------


@dataclass
class RepairOutcome:
    """what happened when we tried to rescue one quarantined record."""

    event_id: str | None
    status: str  # repaired | unrepairable | still_invalid
    rules_applied: list[str]
    reason: str
    event: Event | None = None

    def as_log_row(self, replayed_at: str) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "replayed_at": replayed_at,
            "status": self.status,
            "rules_applied": self.rules_applied,
            "reason": self.reason,
        }


def try_repair(event: Event) -> RepairOutcome:
    """apply every repair that fits, then re-check the contract.

    repairs are cumulative because one record can be broken twice, and passing
    the contract afterwards is non-negotiable. a repair that leaves a record
    still invalid is not a repair, it is a mess with extra steps.
    """
    event_id = event.get("event_id") if isinstance(event, dict) else None

    if not isinstance(event, dict):
        return RepairOutcome(None, "unrepairable", [], "payload is not an object")

    applied: list[str] = []
    working = event

    for rule in _REPAIRS:
        if rule.can_apply(working):
            working = rule.apply(working)
            applied.append(rule.name)

    if not applied:
        failing = validate_event(event)
        codes = sorted({v.code for v in failing.violations})
        return RepairOutcome(
            event_id,
            "unrepairable",
            [],
            f"no repair covers {codes}, and inventing the missing values is not repair",
        )

    outcome = validate_event(working)
    if not outcome.ok:
        codes = sorted({v.code for v in outcome.violations})
        return RepairOutcome(
            event_id,
            "still_invalid",
            applied,
            f"repaired but still failing {codes}",
        )

    return RepairOutcome(event_id, "repaired", applied, "passes the contract", working)


def repair_rules() -> list[tuple[str, str]]:
    """what we know how to fix, for the cli and the docs."""
    return [(r.name, r.describes) for r in _REPAIRS]


@dataclass
class ReplayCounts:
    attempted: int = 0
    repaired: int = 0
    unrepairable: int = 0
    still_invalid: int = 0
    rules_fired: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rules_fired is None:
            self.rules_fired = {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "repaired": self.repaired,
            "unrepairable": self.unrepairable,
            "still_invalid": self.still_invalid,
            "rules_fired": dict(sorted(self.rules_fired.items())),
        }


def replay_quarantine(store: Any, limit: int = 10_000, dry_run: bool = False) -> ReplayCounts:
    """try to rescue everything in quarantine that has not been tried before.

    repaired records are appended to raw exactly as a fresh delivery would be,
    which means staging deduplicates them on event_id like anything else. that
    is what makes a rerun safe: even if the same record were replayed twice, the
    warehouse would still only count it once.

    every attempt is logged, including the refusals, so "why is this record
    still broken" has an answer instead of a shrug.
    """
    counts = ReplayCounts()
    pending = store.read_unreplayed_quarantine(limit=limit)

    replayed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    raw_rows: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []

    for record in pending:
        counts.attempted += 1
        outcome = try_repair(record.get("payload") or {})

        if outcome.status == "repaired":
            counts.repaired += 1
            for rule in outcome.rules_applied:
                counts.rules_fired[rule] = counts.rules_fired.get(rule, 0) + 1
            raw_rows.append(
                {
                    "message_id": f"replay-{record.get('message_id', 'unknown')}",
                    "event_id": outcome.event_id,
                    "schema_version": outcome.event.get("schema_version"),
                    "publish_ts": None,
                    "ingest_ts": replayed_at,
                    "payload": outcome.event,
                }
            )
        elif outcome.status == "still_invalid":
            counts.still_invalid += 1
        else:
            counts.unrepairable += 1

        log_rows.append(outcome.as_log_row(replayed_at))

    if not dry_run:
        store.write_raw(raw_rows)
        store.write_replay_log(log_rows)

    return counts
