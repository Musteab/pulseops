"""Tests for the synthetic generator and the data contract.

The important one is test_clean_runs_produce_no_violations: it proves the
generator and the validator agree on what "valid" means. Without it, a 100
percent detection rate could just mean the validator rejects everything.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from pulseops.contracts import CURRENT_VERSION, validate_event
from pulseops.generator.catalog import MENU_BY_ID, OUTLET_IDS
from pulseops.generator.faults import FAULT_TYPES, detected_by
from pulseops.generator.generate import generate

WINDOW = {"start": date(2026, 6, 1), "end": date(2026, 6, 30)}


def test_clean_runs_produce_no_violations():
    result = generate(n_events=500, seed=1, fault_rate=0.0, **WINDOW)
    assert len(result.events) == 500
    assert result.faults == []
    for event in result.events:
        outcome = validate_event(event)
        assert outcome.ok, (event["event_id"], outcome.as_dict())


def test_same_seed_is_reproducible():
    a = generate(n_events=300, seed=99, fault_rate=0.06, **WINDOW)
    b = generate(n_events=300, seed=99, fault_rate=0.06, **WINDOW)
    assert a.events == b.events
    assert [f.as_dict() for f in a.faults] == [f.as_dict() for f in b.faults]


def test_different_seeds_diverge():
    a = generate(n_events=200, seed=1, fault_rate=0.0, **WINDOW)
    b = generate(n_events=200, seed=2, fault_rate=0.0, **WINDOW)
    assert a.events != b.events


@pytest.mark.parametrize("fault_type", [f for f in FAULT_TYPES if detected_by(f) == "contract"])
def test_contract_faults_are_rejected(fault_type):
    """Every fault tagged `contract` must actually fail validation.

    If a fault is tagged contract but slips through, the tag is a lie and the
    reported detection rate is inflated.
    """
    result = generate(
        n_events=60, seed=5, fault_rate=1.0, fault_types=(fault_type,), **WINDOW
    )
    faulty_ids = {f.event_id for f in result.faults}
    rejected = {
        e["event_id"] for e in result.events
        if "event_id" in e and not validate_event(e).ok
    }
    assert faulty_ids <= rejected, f"{fault_type} was not caught by the contract"


@pytest.mark.parametrize("fault_type", [f for f in FAULT_TYPES if detected_by(f) == "warehouse"])
def test_warehouse_faults_pass_the_contract(fault_type):
    """Faults tagged `warehouse` are structurally valid by design.

    They need reference data or cross-record context, so the contract layer is
    expected to let them through. This test stops someone quietly retagging a
    fault to inflate the contract-layer number.
    """
    result = generate(
        n_events=40, seed=6, fault_rate=1.0, fault_types=(fault_type,), **WINDOW
    )
    for event in result.events:
        assert validate_event(event).ok, f"{fault_type} should pass the contract layer"


def test_duplicate_fault_emits_an_extra_record():
    result = generate(
        n_events=50, seed=3, fault_rate=1.0, fault_types=("duplicate_event",), **WINDOW
    )
    ids = [e["event_id"] for e in result.events]
    assert len(ids) == 100
    assert len(set(ids)) == 50


def test_orphan_fault_points_outside_the_dimension():
    result = generate(
        n_events=25, seed=4, fault_rate=1.0, fault_types=("orphan_menu_item",), **WINDOW
    )
    orphans = [
        line["menu_item_id"]
        for event in result.events
        for line in event["lines"]
        if line["menu_item_id"] not in MENU_BY_ID
    ]
    assert len(orphans) == 25


def test_events_reference_known_outlets_and_items():
    result = generate(n_events=400, seed=11, fault_rate=0.0, **WINDOW)
    for event in result.events:
        assert event["outlet_id"] in OUTLET_IDS
        assert event["schema_version"] == CURRENT_VERSION
        for line in event["lines"]:
            assert line["menu_item_id"] in MENU_BY_ID


def test_totals_reconcile_against_lines():
    result = generate(n_events=400, seed=12, fault_rate=0.0, **WINDOW)
    for event in result.events:
        expected = round(
            sum(line["qty"] * line["unit_price_myr"] for line in event["lines"]), 2
        )
        assert abs(event["order_total_myr"] - expected) < 0.01


def test_failed_payments_capture_nothing():
    result = generate(n_events=3000, seed=13, fault_rate=0.0, **WINDOW)
    failed = [e for e in result.events if e["payment"]["status"] == "failed"]
    assert failed, "expected at least some payment failures in 3000 events"
    assert all(e["payment"]["amount_myr"] == 0.0 for e in failed)


def test_events_stay_inside_the_requested_window():
    result = generate(n_events=500, seed=14, fault_rate=0.0, **WINDOW)
    for event in result.events:
        day = event["event_ts"][:10]
        assert WINDOW["start"].isoformat() <= day <= WINDOW["end"].isoformat()


def test_manifest_counts_match_the_fault_list():
    result = generate(n_events=800, seed=15, fault_rate=0.1, **WINDOW)
    manifest = result.manifest()
    assert manifest["fault_count"] == len(result.faults)
    assert sum(manifest["fault_counts_by_type"].values()) == len(result.faults)
    assert sum(manifest["fault_counts_by_layer"].values()) == len(result.faults)
    assert json.dumps(manifest)  # manifest must be serialisable for the eval suite


def test_rejects_bad_arguments():
    with pytest.raises(ValueError):
        generate(n_events=0)
    with pytest.raises(ValueError):
        generate(n_events=10, fault_rate=1.5)
    with pytest.raises(ValueError):
        generate(n_events=10, fault_types=("not_a_real_fault",))
    with pytest.raises(ValueError):
        generate(n_events=10, start=date(2026, 7, 1), end=date(2026, 6, 1))


def test_validator_reports_every_violation_not_just_the_first():
    broken = {"event_type": "order.placed", "schema_version": CURRENT_VERSION}
    outcome = validate_event(broken)
    assert not outcome.ok
    assert len(outcome.violations) > 3


def test_the_window_does_not_change_what_gets_broken():
    """same seed, different month, identical faults.

    this exists because it was not true. `day.weekday() < 5 and rng.random()`
    short-circuits, so weekends drew no random number and the whole stream
    shifted depending on which days the window contained. `make demo` reported
    185 faults one day and 193 four days later, on the same seed, which made
    every number in the readme unreproducible.
    """
    windows = [
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 8, 3), date(2026, 9, 1)),   # starts on a monday
        (date(2027, 1, 2), date(2027, 1, 31)),  # starts on a saturday
    ]

    manifests = []
    for start, end in windows:
        result = generate(n_events=800, seed=42, fault_rate=0.05, start=start, end=end)
        manifest = result.manifest()
        manifests.append(
            (
                manifest["fault_counts_by_type"],
                manifest["fault_counts_by_layer"],
                len(result.events),
            )
        )

    assert len(set(map(str, manifests))) == 1, f"window changed the outcome: {manifests}"


def test_the_weekend_roll_is_always_drawn():
    """a window made entirely of weekend days must consume the same number of
    random draws as one made entirely of weekdays."""
    saturday_only = generate(
        n_events=200, seed=7, fault_rate=0.0,
        start=date(2027, 1, 2), end=date(2027, 1, 2),
    )
    monday_only = generate(
        n_events=200, seed=7, fault_rate=0.0,
        start=date(2027, 1, 4), end=date(2027, 1, 4),
    )

    # the dates differ, but everything downstream of the draw must line up
    assert [e["order_total_myr"] for e in saturday_only.events] == [
        e["order_total_myr"] for e in monday_only.events
    ]
