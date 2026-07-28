"""Versioned data contract for PulseOps order events.

Every producer writes events that conform to a contract version. Consumers
declare which versions they accept. When a producer starts emitting a shape
the consumer did not agree to, that is a contract violation and the record is
quarantined rather than silently loaded.

This module is the single source of truth for that agreement. The generator
uses it to emit valid events, the ingest layer uses it to reject invalid ones,
and the eval suite uses it to score how many injected faults were caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CURRENT_VERSION = "1.0.0"

CHANNELS = ("dine_in", "takeaway", "delivery")
PAYMENT_METHODS = ("card", "ewallet", "cash", "online_banking")
PAYMENT_STATUSES = ("captured", "pending", "failed", "refunded")


@dataclass(frozen=True)
class FieldRule:
    """One field's expectations inside a contract."""

    name: str
    kind: type | tuple[type, ...]
    required: bool = True
    allowed: tuple[str, ...] | None = None
    min_value: float | None = None
    nullable: bool = False


ORDER_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("event_id", str),
    FieldRule("event_type", str, allowed=("order.placed",)),
    FieldRule("schema_version", str),
    FieldRule("event_ts", str),
    FieldRule("outlet_id", str),
    FieldRule("order_id", str),
    FieldRule("customer_id", str, nullable=True),
    FieldRule("channel", str, allowed=CHANNELS),
    FieldRule("order_total_myr", (int, float), min_value=0.0),
    FieldRule("payment", dict),
    FieldRule("lines", list),
)

PAYMENT_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("method", str, allowed=PAYMENT_METHODS),
    FieldRule("status", str, allowed=PAYMENT_STATUSES),
    FieldRule("amount_myr", (int, float), min_value=0.0),
)

LINE_FIELDS: tuple[FieldRule, ...] = (
    FieldRule("menu_item_id", str),
    FieldRule("qty", int, min_value=1),
    FieldRule("unit_price_myr", (int, float), min_value=0.0),
    FieldRule("line_total_myr", (int, float), min_value=0.0),
)


@dataclass
class Violation:
    """A single reason one event failed the contract."""

    code: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass
class ValidationResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": [v.as_dict() for v in self.violations]}


def _check_fields(
    payload: dict[str, Any], rules: tuple[FieldRule, ...], prefix: str
) -> list[Violation]:
    found: list[Violation] = []

    for rule in rules:
        path = f"{prefix}{rule.name}"

        if rule.name not in payload:
            if rule.required:
                found.append(
                    Violation("missing_field", path, f"required field {rule.name} absent")
                )
            continue

        value = payload[rule.name]

        if value is None:
            if not rule.nullable:
                found.append(Violation("null_value", path, "field is null"))
            continue

        # bool is a subclass of int in python, so reject it explicitly for numerics
        if isinstance(value, bool) and rule.kind is not bool:
            found.append(Violation("type_mismatch", path, "got bool"))
            continue

        if not isinstance(value, rule.kind):
            expected = getattr(rule.kind, "__name__", str(rule.kind))
            found.append(
                Violation(
                    "type_mismatch",
                    path,
                    f"expected {expected}, got {type(value).__name__}",
                )
            )
            continue

        if rule.allowed is not None and value not in rule.allowed:
            found.append(
                Violation("value_not_allowed", path, f"{value!r} not in {rule.allowed}")
            )

        if rule.min_value is not None and isinstance(value, (int, float)):
            if value < rule.min_value:
                found.append(
                    Violation("below_minimum", path, f"{value} < {rule.min_value}")
                )

    return found


def validate_event(event: Any) -> ValidationResult:
    """Check one decoded event against the current contract.

    Returns every violation found rather than failing on the first one, so the
    quarantine table records the full reason an event was rejected.
    """
    if not isinstance(event, dict):
        return ValidationResult(
            False,
            [Violation("not_an_object", "$", f"got {type(event).__name__}")],
        )

    violations = _check_fields(event, ORDER_FIELDS, "")

    declared = event.get("schema_version")
    if isinstance(declared, str) and declared != CURRENT_VERSION:
        violations.append(
            Violation(
                "unsupported_schema_version",
                "schema_version",
                f"consumer accepts {CURRENT_VERSION}, event declared {declared}",
            )
        )

    ts = event.get("event_ts")
    if isinstance(ts, str):
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            violations.append(
                Violation("unparseable_timestamp", "event_ts", f"{ts!r} is not ISO-8601")
            )

    payment = event.get("payment")
    if isinstance(payment, dict):
        violations.extend(_check_fields(payment, PAYMENT_FIELDS, "payment."))

    lines = event.get("lines")
    if isinstance(lines, list):
        if not lines:
            violations.append(Violation("empty_lines", "lines", "order has no line items"))
        for idx, line in enumerate(lines):
            if not isinstance(line, dict):
                violations.append(
                    Violation("type_mismatch", f"lines[{idx}]", "line is not an object")
                )
                continue
            violations.extend(_check_fields(line, LINE_FIELDS, f"lines[{idx}]."))

    violations.extend(_check_arithmetic(event))

    return ValidationResult(ok=not violations, violations=violations)


def _check_arithmetic(event: dict[str, Any]) -> list[Violation]:
    """Business rules that span more than one field."""
    found: list[Violation] = []
    lines = event.get("lines")
    total = event.get("order_total_myr")

    if not isinstance(lines, list) or not isinstance(total, (int, float)):
        return found

    computed = 0.0
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        qty = line.get("qty")
        unit = line.get("unit_price_myr")
        line_total = line.get("line_total_myr")

        if isinstance(qty, int) and isinstance(unit, (int, float)):
            expected = round(qty * unit, 2)
            if isinstance(line_total, (int, float)) and abs(line_total - expected) > 0.01:
                found.append(
                    Violation(
                        "line_total_mismatch",
                        f"lines[{idx}].line_total_myr",
                        f"{line_total} != qty*unit ({expected})",
                    )
                )
        if isinstance(line_total, (int, float)):
            computed += line_total

    if abs(total - round(computed, 2)) > 0.01:
        found.append(
            Violation(
                "order_total_mismatch",
                "order_total_myr",
                f"{total} != sum(line_total) ({round(computed, 2)})",
            )
        )

    return found
