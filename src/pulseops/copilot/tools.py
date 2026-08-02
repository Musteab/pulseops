"""the only things the copilot can do.

every tool is read-only, every one returns its sources, and the sql tool refuses
anything the guard does not approve. there is deliberately no tool for writing,
replaying, or deleting: those are real actions with real consequences and a
human runs them from the cli.

each result carries the tables it touched and the bytes it scanned, because an
answer from an agent that cannot tell you where it came from is not usable in an
incident. "revenue is down" is a rumour. "revenue is down, here is the query,
here are the tables, it scanned 4MB" is something you can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .guard import DEFAULT_ALLOWED_TABLES, SqlGuardError, check_sql

# refuse to run anything that would scan more than this. the copilot is for
# questions, not for accidentally billing someone for a terabyte.
MAX_BYTES_SCANNED = 200 * 1024 * 1024


@dataclass
class ToolResult:
    """what a tool returns, including where it got it."""

    ok: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    sql: str | None = None
    bytes_scanned: int | None = None
    job_id: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            payload["rows"] = self.rows
            payload["row_count"] = len(self.rows)
            payload["sources"] = self.sources
            if self.sql:
                payload["sql"] = self.sql
            if self.bytes_scanned is not None:
                payload["bytes_scanned"] = self.bytes_scanned
            if self.job_id:
                payload["job_id"] = self.job_id
        else:
            payload["error"] = self.error
        return payload


def refuse(reason: str) -> ToolResult:
    return ToolResult(ok=False, error=reason)


class CopilotTools:
    """read-only access to the marts and the quality tables."""

    def __init__(
        self,
        project_id: str,
        client: Any | None = None,
        allowed_tables: frozenset[str] = DEFAULT_ALLOWED_TABLES,
        max_bytes: int = MAX_BYTES_SCANNED,
    ) -> None:
        self.project_id = project_id
        self.allowed_tables = allowed_tables
        self.max_bytes = max_bytes
        self._client = client  # injected in tests, built lazily otherwise

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project_id)
        return self._client

    # -- the general one ---------------------------------------------------

    def run_sql(self, sql: str) -> ToolResult:
        """run a read-only query against the allowlisted tables.

        two gates, in order. the guard decides whether the query is *allowed*,
        then a dry run decides whether it is *affordable*. both refuse before
        anything executes, so a rejected query costs nothing.
        """
        try:
            guarded = check_sql(sql, self.allowed_tables)
        except SqlGuardError as exc:
            return refuse(str(exc))

        try:
            estimate = self._dry_run(guarded.sql)
        except Exception as exc:  # pragma: no cover, depends on live bigquery
            return refuse(f"could not plan the query: {exc}")

        if estimate > self.max_bytes:
            return refuse(
                f"this query would scan {estimate / 1048576:.0f}MB, over the "
                f"{self.max_bytes / 1048576:.0f}MB limit. narrow the date range "
                "or select fewer columns"
            )

        try:
            job = self.client.query(guarded.sql)
            rows = [dict(row) for row in job.result()]
        except Exception as exc:  # pragma: no cover, depends on live bigquery
            return refuse(f"query failed: {exc}")

        return ToolResult(
            ok=True,
            rows=_jsonable(rows),
            sources=list(guarded.tables),
            sql=guarded.sql,
            bytes_scanned=getattr(job, "total_bytes_processed", None),
            job_id=getattr(job, "job_id", None),
        )

    def _dry_run(self, sql: str) -> int:
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = self.client.query(sql, job_config=config)
        return int(job.total_bytes_processed or 0)

    # -- the narrow ones ---------------------------------------------------
    #
    # these exist so the model does not have to write sql for the three
    # questions it will be asked constantly. a named tool with no free-text
    # input cannot be prompt-injected into reading something it should not.

    def quarantine_summary(self, days: int = 30) -> ToolResult:
        """what has been rejected recently, and why.

        the two counts are deliberately named apart. one record can break
        several rules at once, so the violation counts sum to more than the
        number of records and a column called "records" invites anyone reading
        it, human or model, to add them up and report a total that never
        happened. records_affected is the honest denominator.
        """
        return self.run_sql(
            f"""
            select
                code as violation_code,
                count(*) as violation_occurrences,
                count(distinct message_id) as records_affected,
                (
                    select count(distinct message_id)
                    from `{self.project_id}.pulseops_quarantine.orders_quarantine`
                    where quarantined_ts
                        >= timestamp_sub(current_timestamp(), interval {int(days)} day)
                ) as total_quarantined_records
            from `{self.project_id}.pulseops_quarantine.orders_quarantine`,
            unnest(violation_codes) as code
            where quarantined_ts >= timestamp_sub(current_timestamp(), interval {int(days)} day)
            group by 1
            order by violation_occurrences desc
            """
        )

    def warehouse_faults(self) -> ToolResult:
        """the fault classes the ingest contract cannot see."""
        return self.run_sql(
            f"select * from `{self.project_id}.pulseops_mart.dq_warehouse_faults`"
        )

    def replay_history(self, days: int = 30) -> ToolResult:
        """what has been rescued from quarantine, and what was refused."""
        return self.run_sql(
            f"""
            select
                status,
                count(*) as records,
                min(replayed_at) as first_attempt,
                max(replayed_at) as last_attempt
            from `{self.project_id}.pulseops_quarantine.replay_log`
            where replayed_at >= timestamp_sub(current_timestamp(), interval {int(days)} day)
            group by 1
            order by records desc
            """
        )

    def revenue_by_outlet(self, days: int = 30) -> ToolResult:
        """the question the whole project exists to answer honestly."""
        return self.run_sql(
            f"""
            select
                outlet_id,
                order_date,
                round(sum(captured_revenue_myr), 2) as captured_revenue_myr,
                count(distinct event_id) as orders
            from `{self.project_id}.pulseops_mart.fct_order_line`
            where order_date >= date_sub(current_date(), interval {int(days)} day)
            group by 1, 2
            order by order_date desc, captured_revenue_myr desc
            """
        )


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """bigquery hands back dates, decimals and timestamps. models want strings."""
    out = []
    for row in rows:
        clean = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            elif isinstance(value, int | float | str | bool) or value is None:
                clean[key] = value
            else:
                clean[key] = str(value)
        out.append(clean)
    return out
