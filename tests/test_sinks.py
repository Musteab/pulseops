"""tests for the sink layer.

pubsub itself isn't tested here, that needs a real project. what is tested is
everything around it: uri parsing, attributes, and the file sink behaving like
a real sink rather than a stub.
"""

from __future__ import annotations

import json

import pytest

from pulseops.ingest.sinks import (
    FileSink,
    PublishResult,
    message_attributes,
    sink_from_uri,
)

EVENT = {
    "event_id": "abc-123",
    "event_type": "order.placed",
    "schema_version": "1.0.0",
    "outlet_id": "OUT-KL-001",
}


def test_file_sink_writes_one_line_per_event(tmp_path):
    out = tmp_path / "published.jsonl"
    with FileSink(out) as sink:
        for _ in range(3):
            sink.publish(EVENT)

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["data"] == EVENT


def test_file_sink_records_attributes_alongside_the_body(tmp_path):
    out = tmp_path / "published.jsonl"
    with FileSink(out) as sink:
        sink.publish(EVENT)

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["attributes"] == {
        "event_type": "order.placed",
        "schema_version": "1.0.0",
    }


def test_file_sink_returns_unique_message_ids(tmp_path):
    with FileSink(tmp_path / "out.jsonl") as sink:
        ids = [sink.publish(EVENT).message_id for _ in range(5)]
    assert len(set(ids)) == 5


def test_publish_result_carries_a_latency(tmp_path):
    with FileSink(tmp_path / "out.jsonl") as sink:
        result = sink.publish(EVENT)
    assert isinstance(result, PublishResult)
    assert result.latency_ms >= 0


def test_file_sink_creates_missing_directories(tmp_path):
    out = tmp_path / "deep" / "nested" / "out.jsonl"
    with FileSink(out) as sink:
        sink.publish(EVENT)
    assert out.exists()


def test_attributes_survive_a_junk_event():
    """a malformed event still has to be publishable, we validate downstream."""
    assert message_attributes({}) == {
        "event_type": "unknown",
        "schema_version": "unknown",
    }


def test_uri_builds_a_file_sink(tmp_path):
    sink = sink_from_uri(f"file://{tmp_path}/out.jsonl")
    try:
        assert isinstance(sink, FileSink)
        assert sink.path == tmp_path / "out.jsonl"
    finally:
        sink.close()


def test_uri_rejects_nonsense():
    with pytest.raises(ValueError, match="no sink for scheme"):
        sink_from_uri("carrierpigeon://somewhere")
    with pytest.raises(ValueError, match="pubsub://project/topic"):
        sink_from_uri("pubsub://just-a-project")
    with pytest.raises(ValueError, match="file uri needs a path"):
        sink_from_uri("file://")
