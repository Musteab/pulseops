"""tests for the raw and quarantine stores.

bigquery itself needs a real project, so what is tested here is the part that
would otherwise only fail in the cloud: uri parsing, append-only behaviour, and
the json encoding rule that the streaming api is fussy about.
"""

from __future__ import annotations

import json

import pytest

from pulseops.ingest.stores import BigQueryStore, FileStore, store_from_uri

RAW_ROW = {
    "message_id": "m-1",
    "event_id": "evt-1",
    "schema_version": "1.0.0",
    "ingest_ts": "2026-07-29T10:00:00Z",
    "payload": {"event_id": "evt-1", "order_total_myr": 42.5},
}

QUARANTINE_ROW = {
    "message_id": "m-2",
    "event_id": None,
    "quarantined_ts": "2026-07-29T10:00:01Z",
    "violation_codes": ["missing_field"],
    "violations": [{"code": "missing_field", "path": "outlet_id", "detail": "absent"}],
    "payload": {"nope": True},
}


def test_file_store_splits_raw_and_quarantine(tmp_path):
    with FileStore(tmp_path) as store:
        store.write_raw([RAW_ROW])
        store.write_quarantine([QUARANTINE_ROW])

    raw = json.loads(store.raw_path.read_text(encoding="utf-8"))
    quarantined = json.loads(store.quarantine_path.read_text(encoding="utf-8"))
    assert raw["event_id"] == "evt-1"
    assert quarantined["violation_codes"] == ["missing_field"]


def test_writes_append_rather_than_overwrite(tmp_path):
    """raw is append only. a second run must not eat the first one."""
    with FileStore(tmp_path) as store:
        store.write_raw([RAW_ROW])
    with FileStore(tmp_path) as store:
        store.write_raw([RAW_ROW])

    lines = store.raw_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_writing_nothing_is_not_an_error(tmp_path):
    with FileStore(tmp_path) as store:
        assert store.write_raw([]) == 0
        assert store.write_quarantine([]) == 0
    assert not store.raw_path.exists()


def test_row_counts_come_back(tmp_path):
    with FileStore(tmp_path) as store:
        assert store.write_raw([RAW_ROW, RAW_ROW, RAW_ROW]) == 3


def test_json_columns_are_encoded_as_strings():
    """bigquery streaming refuses a dict for a JSON column, so we stringify."""
    encoded = BigQueryStore.encode(RAW_ROW)
    assert isinstance(encoded["payload"], str)
    assert json.loads(encoded["payload"])["order_total_myr"] == 42.5
    # non-json columns are left exactly alone
    assert encoded["event_id"] == "evt-1"


def test_encoding_is_idempotent():
    once = BigQueryStore.encode(QUARANTINE_ROW)
    twice = BigQueryStore.encode(once)
    assert once == twice


def test_encoding_does_not_mutate_the_original():
    original = dict(RAW_ROW)
    BigQueryStore.encode(RAW_ROW)
    assert RAW_ROW == original
    assert isinstance(RAW_ROW["payload"], dict)


def test_uri_builds_a_file_store(tmp_path):
    store = store_from_uri(f"file://{tmp_path}/warehouse")
    assert isinstance(store, FileStore)
    assert store.directory.exists()


def test_uri_rejects_nonsense():
    with pytest.raises(ValueError, match="no store for scheme"):
        store_from_uri("postgres://localhost/whatever")
    with pytest.raises(ValueError, match="bq://project-id"):
        store_from_uri("bq://")
    with pytest.raises(ValueError, match="file uri needs a directory"):
        store_from_uri("file://")
