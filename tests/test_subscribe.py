"""tests for the routing decision.

no pubsub and no bigquery in here. `route` is a pure function over bytes, which
means the part of the pipeline that decides whether your revenue numbers are
real can be tested without a cloud account.
"""

from __future__ import annotations

import json
from datetime import date

from pulseops.generator.generate import generate
from pulseops.ingest.stores import FileStore
from pulseops.ingest.subscribe import QUARANTINE, RAW, route, route_all

WINDOW = {"start": date(2026, 6, 1), "end": date(2026, 6, 30)}


def encode(event: dict) -> bytes:
    return json.dumps(event).encode("utf-8")


def one_valid_event() -> dict:
    return generate(n_events=1, seed=1, fault_rate=0.0, **WINDOW).events[0]


def test_a_clean_event_goes_to_raw():
    destination, row = route(encode(one_valid_event()), "m-1", "2026-07-29T10:00:00Z")
    assert destination == RAW
    assert row["event_id"]
    assert row["schema_version"] == "1.0.0"
    assert row["ingest_ts"]


def test_a_broken_event_goes_to_quarantine_with_reasons():
    bad = one_valid_event()
    del bad["outlet_id"]

    destination, row = route(encode(bad), "m-2", None)
    assert destination == QUARANTINE
    assert "missing_field" in row["violation_codes"]
    assert row["payload"]["order_id"] == bad["order_id"]  # payload kept for replay
    assert row["replayed_at"] is None


def test_schema_drift_is_caught_and_named():
    """the headline fault. it must land in quarantine, not silently as nulls."""
    drifted = one_valid_event()
    drifted["total_amount"] = drifted.pop("order_total_myr")
    drifted["schema_version"] = "2.0.0"

    destination, row = route(encode(drifted), "m-3", None)
    assert destination == QUARANTINE
    assert "unsupported_schema_version" in row["violation_codes"]
    assert row["schema_version"] == "2.0.0"


def test_total_nonsense_still_gets_quarantined_not_dropped():
    destination, row = route(b"\x80 this is not json at all", "m-4", None)
    assert destination == QUARANTINE
    assert row["violation_codes"] == ["undecodable"]
    assert row["event_id"] is None
    assert "raw_bytes" in row["payload"]


def test_json_that_is_not_an_object_is_rejected():
    destination, row = route(b'["a", "list", "not", "an", "event"]', "m-5", None)
    assert destination == QUARANTINE
    assert "not_an_object" in row["violation_codes"]


def test_publish_ts_is_carried_through():
    destination, row = route(encode(one_valid_event()), "m-6", "2026-07-29T09:00:00Z")
    assert row["publish_ts"] == "2026-07-29T09:00:00Z"


def test_nothing_is_ever_dropped(tmp_path):
    """the invariant that matters. every message lands somewhere."""
    result = generate(n_events=300, seed=7, fault_rate=0.2, **WINDOW)
    messages = [(encode(e), f"m-{i}", None) for i, e in enumerate(result.events)]

    with FileStore(tmp_path) as store:
        counts = route_all(messages, store, batch_size=50)

    assert counts.total == len(messages)
    written = sum(
        len(p.read_text(encoding="utf-8").strip().splitlines())
        for p in (store.raw_path, store.quarantine_path)
        if p.exists()
    )
    assert written == len(messages)


def test_contract_faults_all_reach_quarantine(tmp_path):
    """scored against the generator's ground truth, same as the offline path."""
    result = generate(n_events=200, seed=8, fault_rate=0.3, **WINDOW)
    expected = {f.event_id for f in result.faults if f.detected_by == "contract"}

    messages = [(encode(e), f"m-{i}", None) for i, e in enumerate(result.events)]
    with FileStore(tmp_path) as store:
        route_all(messages, store, batch_size=64)

    quarantined = {
        json.loads(line)["event_id"]
        for line in store.quarantine_path.read_text(encoding="utf-8").splitlines()
    }
    assert expected <= quarantined


def test_counts_break_down_by_violation_code(tmp_path):
    result = generate(
        n_events=40, seed=9, fault_rate=1.0, fault_types=("missing_outlet_id",), **WINDOW
    )
    messages = [(encode(e), f"m-{i}", None) for i, e in enumerate(result.events)]

    with FileStore(tmp_path) as store:
        counts = route_all(messages, store, batch_size=10)

    assert counts.raw == 0
    assert counts.quarantine == 40
    assert counts.violation_codes["missing_field"] == 40
    assert json.dumps(counts.as_dict())
