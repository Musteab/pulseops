"""loading the batch sources into raw.

batch idempotency works differently from streaming idempotency, and getting the
two confused is how you end up with a table that doubles every time someone
reruns yesterday.

streaming dedupes on a business key after the fact, because at-least-once
delivery means the same event genuinely arrives twice. batch replaces a whole
partition, because a rerun means the file was wrong and the new one supersedes
it. so these loads use WRITE_TRUNCATE against `table$YYYYMMDD`, which swaps one
day atomically and leaves every other day alone.

the alternative, DELETE then INSERT, is two operations with a window in between
where the day is missing. a partition decorator is one operation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any


def _stamp(rows: list[dict[str, Any]], loaded_at: str) -> list[dict[str, Any]]:
    return [dict(row, loaded_at=loaded_at) for row in rows]


def _partition_key(value: Any) -> str:
    text = str(value)[:10]
    return text.replace("-", "")


def group_by_partition(rows: list[dict[str, Any]], date_field: str) -> dict[str, list[dict]]:
    """split rows by the day they belong to.

    a batch can legitimately span several days, and each one has to be replaced
    independently. loading a three-day file into a single partition would put
    Tuesday's stock under Monday's date.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_partition_key(row[date_field]), []).append(row)
    return grouped


def load_partitioned(
    client: Any,
    table: str,
    rows: list[dict[str, Any]],
    date_field: str,
    schema: Any = None,
) -> dict[str, int]:
    """replace one partition per distinct day in the batch.

    returns rows written per partition, so a caller can assert the shape of
    what happened rather than trusting that it did.
    """
    if not rows:
        return {}

    from google.cloud import bigquery

    # take the schema from the table rather than letting bigquery guess it from
    # the json. autodetect reads a python float as FLOAT64, the table declares
    # unit_cost_myr as NUMERIC, and the load is rejected for changing a column
    # type. asking the table means terraform stays the single definition of the
    # shape and this loader can never disagree with it.
    if schema is None:
        schema = client.get_table(table).schema

    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    stamped = _stamp(rows, loaded_at)
    written: dict[str, int] = {}

    for partition, partition_rows in group_by_partition(stamped, date_field).items():
        config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=schema,
        )
        job = client.load_table_from_json(
            partition_rows, f"{table}${partition}", job_config=config
        )
        job.result()
        written[partition] = len(partition_rows)

    return written


def load_inventory(
    project_id: str, rows: list[dict[str, Any]], client: Any = None
) -> dict[str, int]:
    from google.cloud import bigquery

    client = client or bigquery.Client(project=project_id)
    # is_stockout is derivable from units_on_hand, so it is not stored. a column
    # that can disagree with the column it is derived from eventually will.
    payload = [{k: v for k, v in row.items() if k != "is_stockout"} for row in rows]
    return load_partitioned(
        client, f"{project_id}.pulseops_raw.inventory_raw", payload, "snapshot_date"
    )


def load_weather(
    project_id: str, rows: list[dict[str, Any]], client: Any = None
) -> dict[str, int]:
    from google.cloud import bigquery

    client = client or bigquery.Client(project=project_id)
    return load_partitioned(
        client, f"{project_id}.pulseops_raw.weather_raw", rows, "weather_date"
    )


def today() -> date:
    return datetime.now(UTC).date()
