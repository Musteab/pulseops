"""tests for the replay runner and its idempotency guard.

the guard is the whole reason the replay log exists. replay is the one command
in this project that adds rows to raw after the fact, so a rerun that
double-counted would inflate revenue and nobody would notice until the numbers
were queried.
"""

from __future__ import annotations

import json
from datetime import date

from pulseops.generator.generate import generate
from pulseops.ingest.replay import replay_quarantine
from pulseops.ingest.stores import FileStore
from pulseops.ingest.subscribe import route_all

WINDOW = {"start": date(2026, 6, 1), "end": date(2026, 6, 30)}


def quarantine_some(tmp_path, fault_types, n=30, seed=4):
    """run broken events through the real routing path into a store."""
    result = generate(
        n_events=n, seed=seed, fault_rate=1.0, fault_types=fault_types, **WINDOW
    )
    messages = [
        (json.dumps(e).encode("utf-8"), f"m-{i}", None) for i, e in enumerate(result.events)
    ]
    store = FileStore(tmp_path)
    route_all(messages, store)
    return store


def raw_count(store) -> int:
    if not store.raw_path.exists():
        return 0
    return len(store.raw_path.read_text(encoding="utf-8").strip().splitlines())


def test_schema_drift_records_are_rescued(tmp_path):
    store = quarantine_some(tmp_path, ("schema_drift",))
    assert raw_count(store) == 0  # all of them were rejected on the way in

    counts = replay_quarantine(store)

    assert counts.attempted == 30
    assert counts.repaired == 30
    assert counts.unrepairable == 0
    assert counts.rules_fired == {"v2_total_rename": 30}
    assert raw_count(store) == 30  # and now they are in raw


def test_unrepairable_records_are_logged_not_forced(tmp_path):
    store = quarantine_some(tmp_path, ("missing_outlet_id",))

    counts = replay_quarantine(store)

    assert counts.attempted == 30
    assert counts.repaired == 0
    assert counts.unrepairable == 30
    assert raw_count(store) == 0  # nothing invented, nothing written

    logged = store.replay_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(logged) == 30
    assert all(json.loads(line)["status"] == "unrepairable" for line in logged)


def test_rerunning_replay_changes_nothing(tmp_path):
    """the guard. a second run must find nothing left to do."""
    store = quarantine_some(tmp_path, ("schema_drift",))

    first = replay_quarantine(store)
    after_first = raw_count(store)

    second = replay_quarantine(store)

    assert first.repaired == 30
    assert second.attempted == 0
    assert second.repaired == 0
    assert raw_count(store) == after_first  # no extra rows, no double counting


def test_refusals_are_also_remembered(tmp_path):
    """a refused record must not be retried forever, or every run reprocesses
    the same rubbish and the log grows without bound."""
    store = quarantine_some(tmp_path, ("negative_qty",))

    replay_quarantine(store)
    second = replay_quarantine(store)

    assert second.attempted == 0


def test_mixed_batch_splits_correctly(tmp_path):
    store = quarantine_some(
        tmp_path, ("schema_drift", "missing_outlet_id", "unparseable_timestamp"), n=60, seed=9
    )

    counts = replay_quarantine(store)

    assert counts.attempted == 60
    assert counts.repaired + counts.unrepairable + counts.still_invalid == 60
    assert counts.repaired > 0
    assert counts.unrepairable > 0
    assert raw_count(store) == counts.repaired


def test_dry_run_writes_nothing(tmp_path):
    store = quarantine_some(tmp_path, ("schema_drift",))

    counts = replay_quarantine(store, dry_run=True)

    assert counts.repaired == 30
    assert raw_count(store) == 0
    assert not store.replay_log_path.exists()

    # and because nothing was logged, a real run still has all the work to do
    assert replay_quarantine(store).repaired == 30


def test_replayed_rows_carry_a_traceable_message_id(tmp_path):
    store = quarantine_some(tmp_path, ("schema_drift",))
    replay_quarantine(store)

    first = json.loads(store.raw_path.read_text(encoding="utf-8").splitlines()[0])
    assert first["message_id"].startswith("replay-")
    assert first["payload"]["schema_version"] == "1.0.0"


def test_counts_serialise(tmp_path):
    store = quarantine_some(tmp_path, ("schema_drift",))
    payload = replay_quarantine(store).as_dict()
    assert json.dumps(payload)
    assert payload["repaired"] == 30
