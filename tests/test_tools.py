"""tests for the copilot's tools, with a fake bigquery client.

the point of these is that a refused query must never reach the client at all.
a guard that rejects the response after the delete has already run is not a
guard, so the fake records every statement it is asked to execute and the tests
assert on that record.
"""

from __future__ import annotations

from datetime import date

import pytest

from pulseops.copilot.tools import CopilotTools

MART = "pulseops_mart.fct_order_line"


class FakeJob:
    def __init__(self, rows, bytes_processed):
        self._rows = rows
        self.total_bytes_processed = bytes_processed
        self.job_id = "fake-job-1"

    def result(self):
        return self._rows


class FakeClient:
    """records everything it is asked to run, so tests can prove it was spared."""

    def __init__(self, rows=None, bytes_processed=1024):
        self.rows = rows if rows is not None else [{"outlet_id": "OUT-KL-001", "revenue": 42}]
        self.bytes_processed = bytes_processed
        self.executed: list[str] = []
        self.dry_runs: list[str] = []

    def query(self, sql, job_config=None):
        if job_config is not None and getattr(job_config, "dry_run", False):
            self.dry_runs.append(sql)
            return FakeJob([], self.bytes_processed)
        self.executed.append(sql)
        return FakeJob(self.rows, self.bytes_processed)


@pytest.fixture
def tools():
    return CopilotTools(project_id="test-project", client=FakeClient())


def test_a_legitimate_query_runs_and_reports_its_sources(tools):
    result = tools.run_sql(f"select outlet_id from {MART}")

    assert result.ok
    assert result.sources == [MART]
    assert result.bytes_scanned == 1024
    assert result.job_id == "fake-job-1"


def test_a_write_never_reaches_bigquery(tools):
    """the assertion that matters. refused before execution, not after."""
    result = tools.run_sql(f"delete from {MART} where 1=1")

    assert not result.ok
    assert tools.client.executed == []
    assert tools.client.dry_runs == []


def test_a_forbidden_table_never_reaches_bigquery(tools):
    result = tools.run_sql("select * from pulseops_raw.orders_raw")

    assert not result.ok
    assert "allowlist" in result.error
    assert tools.client.executed == []


def test_an_expensive_query_is_refused_after_planning_but_before_running():
    """dry run costs nothing and stops the query that would cost something."""
    client = FakeClient(bytes_processed=900 * 1024 * 1024)
    tools = CopilotTools(project_id="test-project", client=client)

    result = tools.run_sql(f"select * from {MART}")

    assert not result.ok
    assert "over the" in result.error
    assert client.dry_runs, "should have planned it"
    assert client.executed == [], "but never run it"


def test_the_dry_run_happens_before_the_real_one(tools):
    tools.run_sql(f"select 1 from {MART}")
    assert len(tools.client.dry_runs) == 1
    assert len(tools.client.executed) == 1


def test_a_limit_is_applied_to_an_unbounded_query(tools):
    tools.run_sql(f"select * from {MART}")
    assert "limit 1000" in tools.client.executed[0].lower()


def test_named_tools_produce_allowlisted_sql(tools):
    for call in (
        tools.quarantine_summary,
        tools.warehouse_faults,
        tools.replay_history,
        tools.revenue_by_outlet,
    ):
        result = call()
        assert result.ok, f"{call.__name__} was refused: {result.error}"


def test_named_tools_cannot_be_injected_through_their_arguments(tools):
    """days is cast to int, so a string payload cannot become sql."""
    with pytest.raises(ValueError):
        tools.quarantine_summary(days="30; drop table x")  # type: ignore[arg-type]


def test_results_are_json_safe(tools):
    tools.client.rows = [{"day": date(2026, 7, 29), "revenue": 12.5, "note": None}]
    result = tools.run_sql(f"select 1 from {MART}")

    assert result.rows == [{"day": "2026-07-29", "revenue": 12.5, "note": None}]


def test_failures_serialise_without_leaking_rows(tools):
    payload = tools.run_sql(f"drop table {MART}").as_dict()
    assert payload["ok"] is False
    assert "rows" not in payload
    assert payload["error"]


def test_successes_serialise_with_provenance(tools):
    payload = tools.run_sql(f"select 1 from {MART}").as_dict()
    assert payload["ok"] is True
    assert payload["sources"] == [MART]
    assert payload["sql"]
    assert payload["row_count"] == 1
