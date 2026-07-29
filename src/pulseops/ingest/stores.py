"""where validated events land: raw if they passed, quarantine if they did not.

same trick as the sinks. one interface, a file implementation that CI can run
and a bigquery implementation for the real thing, picked with a uri:

    file://data/warehouse
    bq://my-project

nothing here ever updates or deletes. raw is append only because a pipeline
that rewrites its own history cannot be audited, and quarantine is append only
because the record of what broke is worth more than the disk it costs.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RAW_TABLE = "pulseops_raw.orders_raw"
QUARANTINE_TABLE = "pulseops_quarantine.orders_quarantine"


class Store(ABC):
    """somewhere for rows to land. batched, because per-row writes are a bad time."""

    @abstractmethod
    def write_raw(self, rows: list[dict[str, Any]]) -> int:
        """append rows to the raw layer. returns how many landed."""

    @abstractmethod
    def write_quarantine(self, rows: list[dict[str, Any]]) -> int:
        """append rows to quarantine. returns how many landed."""

    def close(self) -> None:  # noqa: B027, optional hook, not every store buffers
        """flush anything held back."""

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FileStore(Store):
    """two jsonl files in a directory. stands in for two bigquery tables."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.directory / "orders_raw.jsonl"
        self.quarantine_path = self.directory / "orders_quarantine.jsonl"

    def _append(self, path: Path, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        return len(rows)

    def write_raw(self, rows: list[dict[str, Any]]) -> int:
        return self._append(self.raw_path, rows)

    def write_quarantine(self, rows: list[dict[str, Any]]) -> int:
        return self._append(self.quarantine_path, rows)


class BigQueryStore(Store):
    """streams rows into bigquery.

    the JSON columns go over the wire as strings. the streaming api will not
    take a dict for a JSON column, which is a fun thing to discover at runtime
    rather than here, so the encoding happens in one place and stays there.
    """

    JSON_COLUMNS = ("payload", "violations")

    def __init__(
        self,
        project_id: str,
        raw_table: str = RAW_TABLE,
        quarantine_table: str = QUARANTINE_TABLE,
    ) -> None:
        try:
            from google.cloud import bigquery
        except ImportError as exc:  # pragma: no cover, depends on optional extra
            raise RuntimeError(
                "bigquery store needs the gcp extra. install it with:\n"
                '    pip install -e ".[gcp]"'
            ) from exc

        self.project_id = project_id
        self.raw_table = f"{project_id}.{raw_table}"
        self.quarantine_table = f"{project_id}.{quarantine_table}"
        self._client = bigquery.Client(project=project_id)

    @classmethod
    def encode(cls, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for column in cls.JSON_COLUMNS:
            if column in out and not isinstance(out[column], str):
                out[column] = json.dumps(out[column], separators=(",", ":"))
        return out

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        encoded = [self.encode(row) for row in rows]
        errors = self._client.insert_rows_json(table, encoded)
        if errors:
            raise RuntimeError(f"bigquery rejected {len(errors)} rows into {table}: {errors}")
        return len(encoded)

    def write_raw(self, rows: list[dict[str, Any]]) -> int:
        return self._insert(self.raw_table, rows)

    def write_quarantine(self, rows: list[dict[str, Any]]) -> int:
        return self._insert(self.quarantine_table, rows)


def store_from_uri(uri: str) -> Store:
    """build a store from a uri. file://some/directory or bq://project-id."""
    parsed = urlparse(uri)

    if parsed.scheme == "file":
        path = f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
        if not path:
            raise ValueError(f"file uri needs a directory, got {uri!r}")
        return FileStore(path)

    if parsed.scheme == "bq":
        project = parsed.netloc
        if not project:
            raise ValueError(f"bq uri should look like bq://project-id, got {uri!r}")
        return BigQueryStore(project)

    raise ValueError(f"no store for scheme {parsed.scheme!r}, expected file or bq")
