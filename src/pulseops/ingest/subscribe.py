"""pull messages off the queue, hold them to the contract, send them somewhere.

this is where the contract is actually enforced. the publisher deliberately
sends everything, so the first moment anyone checks whether an event is any
good is right here, and the decision is binary: raw if it passed, quarantine
with the full list of reasons if it did not.

nothing is ever dropped. a rejected record keeps its payload so it can be
replayed once whoever broke it upstream has fixed it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import validate_event
from .stores import Store

RAW = "raw"
QUARANTINE = "quarantine"


@dataclass
class RouteCounts:
    """tally of where things went."""

    raw: int = 0
    quarantine: int = 0
    undecodable: int = 0
    violation_codes: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.raw + self.quarantine

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "raw": self.raw,
            "quarantine": self.quarantine,
            "undecodable": self.undecodable,
            "violation_codes": dict(sorted(self.violation_codes.items())),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def route(
    data: bytes,
    message_id: str,
    publish_ts: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """decide where one message belongs and build the row for it.

    pure function on purpose. no network, no clients, no cloud. the entire
    routing decision can be tested on a laptop with no credentials, which is
    the only reason this logic is trustworthy.
    """
    now = _now()

    try:
        event = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # not even json. cannot extract an id, cannot validate, so it goes to
        # quarantine with the bytes preserved as text for whoever investigates.
        return QUARANTINE, {
            "message_id": message_id,
            "event_id": None,
            "schema_version": None,
            "publish_ts": publish_ts,
            "quarantined_ts": now,
            "violation_codes": ["undecodable"],
            "violations": [
                {"code": "undecodable", "path": "$", "detail": "payload is not valid json"}
            ],
            "payload": {"raw_bytes": data.decode("utf-8", errors="replace")},
            "replayed_at": None,
        }

    event_id = event.get("event_id") if isinstance(event, dict) else None
    schema_version = event.get("schema_version") if isinstance(event, dict) else None
    outcome = validate_event(event)

    if outcome.ok:
        return RAW, {
            "message_id": message_id,
            "event_id": event_id,
            "schema_version": schema_version,
            "publish_ts": publish_ts,
            "ingest_ts": now,
            "payload": event,
        }

    violations = [v.as_dict() for v in outcome.violations]
    return QUARANTINE, {
        "message_id": message_id,
        "event_id": event_id,
        "schema_version": schema_version,
        "publish_ts": publish_ts,
        "quarantined_ts": now,
        # flat codes as well as the full detail, so "group by failure mode" is a
        # cheap query instead of a json parse across the whole table
        "violation_codes": sorted({v["code"] for v in violations}),
        "violations": violations,
        "payload": event,
        "replayed_at": None,
    }


def route_all(
    messages: list[tuple[bytes, str, str | None]],
    store: Store,
    batch_size: int = 500,
) -> RouteCounts:
    """route a pile of messages and write them out in batches."""
    counts = RouteCounts()
    raw_rows: list[dict[str, Any]] = []
    quarantine_rows: list[dict[str, Any]] = []

    def flush() -> None:
        if raw_rows:
            store.write_raw(raw_rows)
            raw_rows.clear()
        if quarantine_rows:
            store.write_quarantine(quarantine_rows)
            quarantine_rows.clear()

    for data, message_id, publish_ts in messages:
        destination, row = route(data, message_id, publish_ts)
        if destination == RAW:
            raw_rows.append(row)
            counts.raw += 1
        else:
            quarantine_rows.append(row)
            counts.quarantine += 1
            for code in row["violation_codes"]:
                counts.violation_codes[code] = counts.violation_codes.get(code, 0) + 1
                if code == "undecodable":
                    counts.undecodable += 1

        if len(raw_rows) >= batch_size or len(quarantine_rows) >= batch_size:
            flush()

    flush()
    return counts


def pull_and_route(
    project_id: str,
    subscription_id: str,
    store: Store,
    max_messages: int = 5000,
    idle_timeout_s: float = 20.0,
    batch_size: int = 500,
) -> RouteCounts:
    """drain a pubsub subscription into the store.

    synchronous pull in a loop rather than the streaming client. slower, but it
    finishes and tells you what it did, which is what a batch drain wants. the
    streaming pull is built to run forever and is a worse fit here.

    messages are only acked once their rows are safely written. crash halfway
    and pubsub redelivers, which is why raw has to be deduplicated downstream
    on event_id rather than trusted to be unique.
    """
    from google.cloud import pubsub_v1

    client = pubsub_v1.SubscriberClient()
    path = client.subscription_path(project_id, subscription_id)

    counts = RouteCounts()
    empty_polls = 0

    while counts.total < max_messages:
        response = client.pull(
            request={"subscription": path, "max_messages": min(batch_size, 1000)},
            timeout=idle_timeout_s,
        )

        if not response.received_messages:
            empty_polls += 1
            if empty_polls >= 2:  # two empty polls in a row means the queue is drained
                break
            continue

        empty_polls = 0
        batch = [
            (
                m.message.data,
                m.message.message_id,
                m.message.publish_time.rfc3339() if m.message.publish_time else None,
            )
            for m in response.received_messages
        ]

        batch_counts = route_all(batch, store, batch_size=batch_size)
        counts.raw += batch_counts.raw
        counts.quarantine += batch_counts.quarantine
        counts.undecodable += batch_counts.undecodable
        for code, n in batch_counts.violation_codes.items():
            counts.violation_codes[code] = counts.violation_codes.get(code, 0) + n

        # ack only after the write succeeded. losing an ack is cheap, losing a
        # record is not.
        client.acknowledge(
            request={
                "subscription": path,
                "ack_ids": [m.ack_id for m in response.received_messages],
            }
        )

    return counts
