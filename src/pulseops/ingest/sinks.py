"""where events go when we publish them.

one interface, two implementations. the file sink is a real sink, not a mock,
it just writes jsonl to disk. that keeps every test and the whole CI run
working with no cloud account, no credentials and no bill.

pubsub is picked with a uri instead of a pile of flags:

    file://data/raw/published.jsonl
    pubsub://my-project/pulseops-orders

swapping the pipeline from local to cloud is one env var, which is the point.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class PublishResult:
    """what came back from publishing one event."""

    message_id: str
    latency_ms: float


class Sink(ABC):
    """somewhere to put events. must be usable as a context manager."""

    @abstractmethod
    def publish(self, event: dict[str, Any]) -> PublishResult:
        """send one event. blocks until the broker has actually taken it."""

    def close(self) -> None:  # noqa: B027, optional hook, not every sink buffers
        """flush anything buffered. override if the sink buffers."""

    def __enter__(self) -> Sink:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def message_attributes(event: dict[str, Any]) -> dict[str, str]:
    """the bits a subscriber can filter on without decoding the payload.

    pubsub charges you for delivery either way, but a subscriber that only
    wants v1 events shouldn't have to parse a v2 body to find that out.
    """
    return {
        "event_type": str(event.get("event_type", "unknown")),
        "schema_version": str(event.get("schema_version", "unknown")),
    }


class FileSink(Sink):
    """appends jsonl to a file. the offline sink, and what CI uses."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._seq = 0

    def publish(self, event: dict[str, Any]) -> PublishResult:
        started = time.perf_counter()
        payload = {
            "attributes": message_attributes(event),
            "data": event,
        }
        self._fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
        self._seq += 1
        elapsed = (time.perf_counter() - started) * 1000
        # fake but stable message ids, so downstream code can key on them
        return PublishResult(message_id=f"file-{self._seq:09d}", latency_ms=elapsed)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


class PubSubSink(Sink):
    """the real one. publishes to a pubsub topic and waits for the ack.

    google's client batches under the hood and hands back a future. we block on
    it per event, which is slower than fire and forget but means the latency
    numbers we report are honest: time until pubsub actually owned the message,
    not time until we handed it to a buffer.
    """

    def __init__(self, project_id: str, topic_id: str) -> None:
        try:
            from google.cloud import pubsub_v1
        except ImportError as exc:  # pragma: no cover, depends on optional extra
            raise RuntimeError(
                "pubsub sink needs the gcp extra. install it with:\n"
                '    pip install -e ".[gcp]"'
            ) from exc

        self.project_id = project_id
        self.topic_id = topic_id
        self._client = pubsub_v1.PublisherClient()
        self._topic_path = self._client.topic_path(project_id, topic_id)

    def publish(self, event: dict[str, Any]) -> PublishResult:
        body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        started = time.perf_counter()
        future = self._client.publish(self._topic_path, body, **message_attributes(event))
        message_id = future.result()
        elapsed = (time.perf_counter() - started) * 1000
        return PublishResult(message_id=message_id, latency_ms=elapsed)

    def close(self) -> None:
        # publish() already blocks per event, so there is nothing left in flight
        pass


def sink_from_uri(uri: str) -> Sink:
    """build a sink from a uri. see the module docstring for the shapes."""
    parsed = urlparse(uri)

    if parsed.scheme == "file":
        path = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
        if not path:
            raise ValueError(f"file uri needs a path, got {uri!r}")
        return FileSink(path)

    if parsed.scheme == "pubsub":
        project = parsed.netloc
        topic = parsed.path.lstrip("/")
        if not project or not topic:
            raise ValueError(
                f"pubsub uri should look like pubsub://project/topic, got {uri!r}"
            )
        return PubSubSink(project, topic)

    raise ValueError(f"no sink for scheme {parsed.scheme!r}, expected file or pubsub")
