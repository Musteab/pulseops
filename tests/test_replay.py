"""tests for quarantine repair.

the ones that matter most are the refusals. it is easy to write a repair layer
that quietly fabricates values to make the numbers look healthy, and these tests
exist to make sure this one never does.
"""

from __future__ import annotations

from datetime import date

import pytest

from pulseops.contracts import CURRENT_VERSION, validate_event
from pulseops.generator.faults import apply_fault
from pulseops.generator.generate import generate
from pulseops.ingest.replay import repair_rules, try_repair

WINDOW = {"start": date(2026, 6, 1), "end": date(2026, 6, 30)}


def broken_by(fault_type: str, seed: int = 3):
    """one event, broken exactly one way, as the generator would break it."""
    result = generate(
        n_events=1, seed=seed, fault_rate=1.0, fault_types=(fault_type,), **WINDOW
    )
    return result.events[0]


def test_schema_drift_is_repairable():
    """the headline fault. the total is still there, just under another name."""
    outcome = try_repair(broken_by("schema_drift"))

    assert outcome.status == "repaired"
    assert outcome.rules_applied == ["v2_total_rename"]
    assert outcome.event is not None
    assert outcome.event["schema_version"] == CURRENT_VERSION
    assert "total_amount" not in outcome.event
    assert validate_event(outcome.event).ok


def test_repair_preserves_the_actual_value():
    """the whole point is recovering the money, not just passing validation."""
    original = generate(n_events=1, seed=8, fault_rate=0.0, **WINDOW).events[0]
    expected_total = original["order_total_myr"]

    import random

    drifted, _ = apply_fault("schema_drift", original, random.Random(0))
    outcome = try_repair(drifted[0])

    assert outcome.status == "repaired"
    assert outcome.event["order_total_myr"] == expected_total


def test_locale_timestamp_is_repairable():
    outcome = try_repair(broken_by("unparseable_timestamp"))

    assert outcome.status == "repaired"
    assert outcome.rules_applied == ["locale_timestamp"]
    assert validate_event(outcome.event).ok


def test_locale_timestamp_reads_day_first():
    """29/07 is 29 July, not an invalid month. getting this backwards would
    silently move revenue between months."""
    event = generate(n_events=1, seed=2, fault_rate=0.0, **WINDOW).events[0]
    event["event_ts"] = "29/07/2026 02:15 PM"

    outcome = try_repair(event)
    assert outcome.event["event_ts"].startswith("2026-07-29T14:15")


@pytest.mark.parametrize(
    "fault_type",
    [
        "missing_outlet_id",
        "null_order_total",
        "negative_unit_price",
        "negative_qty",
        "empty_lines",
        "unknown_channel",
        "line_total_mismatch",
    ],
)
def test_genuinely_missing_data_is_refused(fault_type):
    """no repair may invent a value that was never sent.

    if one of these ever starts returning "repaired", someone has taught the
    pipeline to make numbers up, and every figure downstream becomes fiction.
    """
    outcome = try_repair(broken_by(fault_type))

    assert outcome.status == "unrepairable"
    assert outcome.event is None
    assert outcome.rules_applied == []
    assert "not repair" in outcome.reason


def test_refusal_says_what_was_wrong():
    outcome = try_repair(broken_by("missing_outlet_id"))
    assert "missing_field" in outcome.reason


def test_repairing_a_clean_event_is_a_no_op():
    clean = generate(n_events=1, seed=5, fault_rate=0.0, **WINDOW).events[0]
    outcome = try_repair(clean)
    assert outcome.status == "unrepairable"  # nothing to repair, nothing claimed


def test_repair_is_idempotent():
    """repairing twice must not change the value, because replay can rerun."""
    once = try_repair(broken_by("schema_drift"))
    assert once.status == "repaired"

    # feed the repaired event straight back in. no rule should match it now,
    # and critically the total must be untouched: a second pass that shifted
    # the number would corrupt every record on a rerun.
    twice = try_repair(once.event)
    assert twice.rules_applied == []
    assert once.event["order_total_myr"] == broken_by("schema_drift")["total_amount"]


def test_repair_does_not_mutate_its_input():
    original = broken_by("schema_drift")
    before = dict(original)
    try_repair(original)
    assert original == before


def test_garbage_is_refused_not_crashed():
    assert try_repair("not even a dict").status == "unrepairable"  # type: ignore[arg-type]
    assert try_repair({}).status == "unrepairable"


def test_every_rule_is_documented():
    for name, describes in repair_rules():
        assert name and describes
        assert len(describes) > 20, f"{name} needs a real description"
