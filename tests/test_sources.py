"""tests for the batch sources.

no network and no bigquery. the weather client takes an injectable fetcher, so
open-meteo's actual availability never decides whether CI passes, and the
loader's partitioning logic is a pure function over rows.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from pulseops.sources.inventory import (
    generate_inventory,
    to_csv_rows,
    validate_inventory_rows,
)
from pulseops.sources.load import group_by_partition
from pulseops.sources.weather import (
    WeatherFetchError,
    build_url,
    fetch_weather,
    parse_response,
    validate_weather_rows,
)

GOOD_RESPONSE = json.dumps(
    {
        "daily": {
            "time": ["2026-08-01", "2026-08-02"],
            "temperature_2m_max": [31.6, 34.0],
            "temperature_2m_min": [24.1, 24.8],
            "precipitation_sum": [7.7, 3.0],
        }
    }
)


# ---------------------------------------------------------------------------
# weather
# ---------------------------------------------------------------------------


def test_a_normal_response_becomes_rows():
    rows = parse_response("Kuala Lumpur", GOOD_RESPONSE, "2026-08-02T10:00:00Z")
    assert len(rows) == 2
    assert rows[0] == {
        "city": "Kuala Lumpur",
        "weather_date": "2026-08-01",
        "temp_max_c": 31.6,
        "temp_min_c": 24.1,
        "precipitation_mm": 7.7,
        "fetched_at": "2026-08-02T10:00:00Z",
        "source": "open-meteo",
    }


def test_mismatched_arrays_are_refused_not_zipped():
    """open-meteo returns parallel arrays. zipping to the shortest would
    silently attach tuesday's temperature to monday's date."""
    broken = json.dumps(
        {
            "daily": {
                "time": ["2026-08-01", "2026-08-02"],
                "temperature_2m_max": [31.6],
                "temperature_2m_min": [24.1, 24.8],
                "precipitation_sum": [7.7, 3.0],
            }
        }
    )
    with pytest.raises(WeatherFetchError, match="different lengths"):
        parse_response("Kuala Lumpur", broken, "now")


def test_junk_responses_are_refused():
    with pytest.raises(WeatherFetchError, match="not json"):
        parse_response("KL", "<html>502 bad gateway</html>", "now")
    with pytest.raises(WeatherFetchError, match="no daily block"):
        parse_response("KL", json.dumps({"error": True}), "now")


def test_one_failing_city_does_not_lose_the_others():
    def flaky(url: str) -> str:
        if "5.414" in url:  # georgetown
            raise WeatherFetchError("Georgetown: timeout")
        return GOOD_RESPONSE

    rows = fetch_weather(
        cities={"Kuala Lumpur": (3.139, 101.687), "Georgetown": (5.414, 100.329)},
        fetcher=flaky,
    )
    assert {r["city"] for r in rows} == {"Kuala Lumpur"}


def test_every_city_failing_is_an_error():
    def dead(url: str) -> str:
        raise WeatherFetchError("nope")

    with pytest.raises(WeatherFetchError, match="every city failed"):
        fetch_weather(cities={"KL": (1.0, 2.0)}, fetcher=dead)


def test_the_url_asks_for_what_we_parse():
    url = build_url(3.139, 101.687, past_days=7)
    for field in ("temperature_2m_max", "temperature_2m_min", "precipitation_sum"):
        assert field in url
    assert "past_days=7" in url


def test_past_days_is_clamped():
    """open-meteo rejects absurd ranges, so do not send them."""
    assert "past_days=0" in build_url(1.0, 2.0, past_days=-5)
    assert "past_days=92" in build_url(1.0, 2.0, past_days=9999)


def test_weather_validation_catches_nonsense():
    rows = parse_response("KL", GOOD_RESPONSE, "now")
    assert validate_weather_rows(rows) == []

    rows[0]["temp_max_c"] = 400  # a unit change, or a sensor on fire
    rows[1]["precipitation_mm"] = -2
    problems = validate_weather_rows(rows)
    assert any("outside" in p for p in problems)
    assert any("negative rainfall" in p for p in problems)


def test_min_above_max_is_caught():
    rows = parse_response("KL", GOOD_RESPONSE, "now")
    rows[0]["temp_min_c"] = 99.0
    assert any("above max" in p for p in validate_weather_rows(rows))


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def test_inventory_covers_every_outlet_and_item():
    rows = generate_inventory(snapshot_date=date(2026, 8, 2), days=1)
    assert len(rows) == 5 * 20
    assert validate_inventory_rows(rows) == []


def test_inventory_is_deterministic():
    a = generate_inventory(snapshot_date=date(2026, 8, 2), days=3, seed=1)
    b = generate_inventory(snapshot_date=date(2026, 8, 2), days=3, seed=1)
    assert a == b


def test_a_different_seed_changes_the_stock():
    a = generate_inventory(snapshot_date=date(2026, 8, 2), days=1, seed=1)
    b = generate_inventory(snapshot_date=date(2026, 8, 2), days=1, seed=2)
    assert a != b


def test_stock_is_never_negative():
    rows = generate_inventory(snapshot_date=date(2026, 8, 2), days=10, seed=99)
    assert all(r["units_on_hand"] >= 0 for r in rows)


def test_stockout_flag_agrees_with_the_number():
    rows = generate_inventory(snapshot_date=date(2026, 8, 2), days=10, seed=5)
    assert all(r["is_stockout"] == (r["units_on_hand"] == 0) for r in rows)
    assert any(r["is_stockout"] for r in rows), "expected some stockouts in 10 days"


def test_a_disagreeing_stockout_flag_is_caught():
    rows = generate_inventory(snapshot_date=date(2026, 8, 2), days=1)
    rows[0]["is_stockout"] = not rows[0]["is_stockout"]
    assert any("disagrees" in p for p in validate_inventory_rows(rows))


def test_duplicate_snapshot_rows_are_caught():
    """the classic batch failure: the same file loaded twice."""
    rows = generate_inventory(snapshot_date=date(2026, 8, 2), days=1)
    doubled = rows + rows
    problems = validate_inventory_rows(doubled)
    assert len(problems) == len(rows)
    assert all("duplicate" in p for p in problems)


def test_csv_shape_drops_the_derived_column():
    rows = to_csv_rows(generate_inventory(snapshot_date=date(2026, 8, 2), days=1))
    assert "is_stockout" not in rows[0]
    assert set(rows[0]) == {
        "snapshot_date", "outlet_id", "menu_item_id", "units_on_hand", "unit_cost_myr",
    }


# ---------------------------------------------------------------------------
# partitioned loading
# ---------------------------------------------------------------------------


def test_rows_are_grouped_by_day():
    rows = generate_inventory(snapshot_date=date(2026, 8, 2), days=3)
    grouped = group_by_partition(rows, "snapshot_date")
    assert set(grouped) == {"20260731", "20260801", "20260802"}
    assert all(len(v) == 100 for v in grouped.values())


def test_grouping_handles_a_timestamp_shaped_date():
    rows = [{"weather_date": "2026-08-02T00:00:00Z"}]
    assert set(group_by_partition(rows, "weather_date")) == {"20260802"}


def test_grouping_nothing_gives_nothing():
    assert group_by_partition([], "snapshot_date") == {}
