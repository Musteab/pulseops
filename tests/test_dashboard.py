"""tests for the dashboard builder.

build_html takes rows in and returns a string, so the whole page can be built
and inspected without touching bigquery. the charts are the part worth testing:
an svg that silently draws a bar off the edge still renders, it is just wrong.
"""

from __future__ import annotations

import pytest

from pulseops.dashboard import Panel, _bar_chart, _line_chart, _money, _short_ts, build_html

DAILY = [
    {"order_date": "2026-07-27", "revenue": 6522.1, "orders": 143},
    {"order_date": "2026-07-28", "revenue": 6298.6, "orders": 143},
    {"order_date": "2026-07-29", "revenue": 7848.7, "orders": 175},
]

OUTLETS = [
    {"outlet_id": "OUT-KL-001", "revenue": 72001.0, "orders": 1600},
    {"outlet_id": "OUT-PG-001", "revenue": 23568.0, "orders": 520},
]

QUARANTINE = [
    {"violation_code": "line_total_mismatch", "violation_occurrences": 56,
     "records_affected": 56, "total_quarantined_records": 185},
    {"violation_code": "missing_field", "violation_occurrences": 46,
     "records_affected": 46, "total_quarantined_records": 185},
]

FAULTS = [
    {"fault_type": "duplicate_event", "events_affected": 22,
     "why_ingest_cannot_see_it": "needs the other records"},
]

REPLAY = [
    {"status": "repaired", "records": 40,
     "last_attempt": "2026-08-01T21:46:51.458471+00:00"},
    {"status": "unrepairable", "records": 145,
     "last_attempt": "2026-08-01T21:46:51.458471+00:00"},
]


def panels() -> dict[str, Panel]:
    return {
        "daily": Panel("d", DAILY, ["pulseops_mart.fct_order_line"]),
        "outlets": Panel("o", OUTLETS, ["pulseops_mart.fct_order_line"]),
        "quarantine": Panel("q", QUARANTINE, ["pulseops_quarantine.orders_quarantine"]),
        "faults": Panel("f", FAULTS, ["pulseops_mart.dq_warehouse_faults"]),
        "replay": Panel("r", REPLAY, ["pulseops_quarantine.replay_log"]),
    }


def test_the_page_builds_and_is_self_contained():
    page = build_html(panels(), "pulseops-muste")
    assert page.startswith("<!doctype html>")
    # no external requests. a dashboard that needs the internet is not a file.
    for offender in ("http://", "https://", "<script"):
        assert offender not in page


def test_headline_numbers_come_from_the_rows():
    page = build_html(panels(), "pulseops-muste")
    assert "20,669" in page  # 6522.1 + 6298.6 + 7848.7
    assert "461" in page  # orders
    assert "185" in page  # quarantined
    assert "40" in page  # recovered


def test_quarantine_note_explains_the_double_count():
    """the bars sum to more than the record count, and the page has to say so
    or it repeats the exact mistake the copilot made."""
    page = build_html(panels(), "pulseops-muste")
    assert "counting violations is" in page
    assert "not counting records" in page


def test_sources_are_listed():
    page = build_html(panels(), "pulseops-muste")
    for table in (
        "pulseops_mart.fct_order_line",
        "pulseops_quarantine.orders_quarantine",
        "pulseops_quarantine.replay_log",
    ):
        assert table in page


def test_labels_are_escaped():
    """row values come from the warehouse, so they are data, not markup."""
    nasty = [{"outlet_id": "<script>alert(1)</script>", "revenue": 1.0}]
    svg = _bar_chart(nasty, "outlet_id", "revenue", "#000")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_bars_never_exceed_the_track():
    svg = _bar_chart(OUTLETS, "outlet_id", "revenue", "#000")
    widths = [int(part.split('"')[0]) for part in svg.split('width="')[1:]]
    track = 720 - 190 - 78
    assert max(widths) <= track


def test_a_zero_row_does_not_vanish_entirely():
    """a bar of width zero looks like missing data rather than a real zero."""
    svg = _bar_chart(
        [{"k": "a", "v": 100}, {"k": "b", "v": 0}], "k", "v", "#000"
    )
    assert 'width="2"' in svg


def test_charts_handle_thin_input():
    assert "nothing to show" in _bar_chart([], "k", "v", "#000")
    assert "not enough days" in _line_chart(DAILY[:1], "order_date", "revenue")


def test_the_line_chart_marks_the_peak():
    svg = _line_chart(DAILY, "order_date", "revenue")
    assert "7,849" in svg  # the highest day, rounded
    assert "<circle" in svg


def test_timestamps_are_shortened():
    assert _short_ts("2026-08-01T21:46:51.458471+00:00") == "2026-08-01 21:46:51"
    assert _short_ts(None) == ""
    assert _short_ts("already short") == "already short"


@pytest.mark.parametrize(
    ("value", "expected"), [(0, "0"), (1234.6, "1,235"), (214698.0, "214,698")]
)
def test_numbers_are_readable(value, expected):
    assert _money(value) == expected
