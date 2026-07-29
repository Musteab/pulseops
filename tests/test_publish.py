"""tests for the publisher and its stats."""

from __future__ import annotations

import json

import pytest

from pulseops.ingest.publish import PublishStats, publish_events, read_events
from pulseops.ingest.sinks import FileSink, PublishResult, Sink

EVENTS = [
    {"event_id": f"evt-{i}", "event_type": "order.placed", "schema_version": "1.0.0"}
    for i in range(10)
]


class FlakySink(Sink):
    """blows up on every nth event, so we can check the run survives it."""

    def __init__(self, fail_every: int) -> None:
        self.fail_every = fail_every
        self.seen = 0

    def publish(self, event):
        self.seen += 1
        if self.seen % self.fail_every == 0:
            raise RuntimeError("broker said no")
        return PublishResult(message_id=f"ok-{self.seen}", latency_ms=1.0)


def test_publishes_everything_including_the_broken_ones(tmp_path):
    """the publisher must not filter. validation is a downstream job."""
    broken = [{"event_id": "nope"}, {"lines": []}, {}]
    out = tmp_path / "out.jsonl"
    with FileSink(out) as sink:
        stats = publish_events(EVENTS + broken, sink)

    assert stats.published == 13
    assert stats.failed == 0
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 13


def test_a_failing_event_does_not_kill_the_run():
    sink = FlakySink(fail_every=3)
    stats = publish_events(EVENTS, sink)
    assert stats.published == 7
    assert stats.failed == 3


def test_limit_stops_early(tmp_path):
    with FileSink(tmp_path / "out.jsonl") as sink:
        stats = publish_events(EVENTS, sink, limit=4)
    assert stats.published == 4


def test_percentiles_are_ordered():
    stats = PublishStats(
        published=100, failed=0, elapsed_s=1.0,
        latencies_ms=[float(i) for i in range(1, 101)],
    )
    assert stats.percentile(50) <= stats.percentile(95) <= stats.percentile(99)
    assert stats.percentile(100) == 100.0


def test_percentiles_on_an_empty_run_do_not_explode():
    stats = PublishStats(published=0, failed=0, elapsed_s=0.0, latencies_ms=[])
    assert stats.percentile(95) == 0.0
    assert stats.throughput_per_s == 0.0
    assert stats.as_dict()["latency_ms"]["max"] == 0.0


def test_throughput_is_events_over_seconds():
    stats = PublishStats(published=500, failed=0, elapsed_s=2.0, latencies_ms=[1.0])
    assert stats.throughput_per_s == 250.0


def test_read_events_skips_blank_lines(tmp_path):
    src = tmp_path / "events.jsonl"
    src.write_text('{"a": 1}\n\n{"a": 2}\n\n', encoding="utf-8")
    assert list(read_events(src)) == [{"a": 1}, {"a": 2}]


def test_read_events_names_the_bad_line(tmp_path):
    src = tmp_path / "events.jsonl"
    src.write_text('{"a": 1}\nnot json at all\n', encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        list(read_events(src))


def test_stats_serialise_for_the_readme(tmp_path):
    with FileSink(tmp_path / "out.jsonl") as sink:
        stats = publish_events(EVENTS, sink)
    payload = stats.as_dict()
    assert json.dumps(payload)
    assert payload["published"] == 10
    assert set(payload["latency_ms"]) == {"p50", "p95", "p99", "max"}
