"""push events at a sink and keep score while doing it.

deliberately publishes everything, including the events the generator broke on
purpose. it is tempting to validate here and drop the bad ones, but a producer
you control is not a producer. real tills emit whatever they feel like, and a
pipeline that can only receive valid data has quietly assumed away the entire
problem this project exists to study. validation happens on the way out of the
queue, not on the way in.

the timing numbers are measured per event and reported as percentiles, because
a mean hides exactly the tail you care about.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sinks import Sink


@dataclass(frozen=True)
class PublishStats:
    """what happened during one publish run."""

    published: int
    failed: int
    elapsed_s: float
    latencies_ms: list[float]

    @property
    def throughput_per_s(self) -> float:
        return self.published / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def percentile(self, p: float) -> float:
        """nearest-rank percentile. no interpolation, no numpy, no drama."""
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        rank = max(1, min(len(ordered), round(p / 100 * len(ordered))))
        return ordered[rank - 1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "failed": self.failed,
            "elapsed_s": round(self.elapsed_s, 3),
            "throughput_per_s": round(self.throughput_per_s, 1),
            "latency_ms": {
                "p50": round(self.percentile(50), 2),
                "p95": round(self.percentile(95), 2),
                "p99": round(self.percentile(99), 2),
                "max": round(max(self.latencies_ms), 2) if self.latencies_ms else 0.0,
            },
        }


def read_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """stream events off disk. skips blank lines, chokes loudly on bad json.

    malformed json here means the generator wrote something broken, which is a
    bug on our side rather than a data-quality fault, so it should not be
    quietly swallowed.
    """
    with Path(path).open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_no} of {path} is not valid json") from exc


def publish_events(
    events: Iterable[dict[str, Any]],
    sink: Sink,
    limit: int | None = None,
    progress_every: int = 0,
) -> PublishStats:
    """publish everything at the sink, timing each one.

    a failure on a single event is counted and stepped over rather than killing
    the run. losing 4999 events because number 4998 upset the broker would be a
    poor trade.
    """
    latencies: list[float] = []
    failed = 0
    started = time.perf_counter()

    for count, event in enumerate(events, start=1):
        if limit is not None and count > limit:
            break
        try:
            latencies.append(sink.publish(event).latency_ms)
        except Exception:  # noqa: BLE001, one bad event should not end the run
            failed += 1

        if progress_every and count % progress_every == 0:
            print(f"  published {count}", flush=True)

    return PublishStats(
        published=len(latencies),
        failed=failed,
        elapsed_s=time.perf_counter() - started,
        latencies_ms=latencies,
    )
