"""daily weather per outlet city, from open-meteo.

this is the one source in the project that is not synthetic. open-meteo is free
and needs no api key, which makes it a genuine third-party dependency: it can
be slow, it can rate limit, it can change its response shape, and none of that
is under our control. that is the point of having it. a pipeline whose every
source is a file you wrote yourself has never had to handle a bad Tuesday.

coordinates live here rather than on dim_outlet because they are a detail of
how this connector fetches, not a fact about the restaurant. the join back to
the warehouse is on city, which dim_outlet already has.

no requests dependency: urllib does this fine and the project has no runtime
dependencies at all, which is worth keeping.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
TIMEZONE = "Asia/Kuala_Lumpur"

# every city the outlets sit in. dim_outlet.city is the join key.
CITY_COORDS: dict[str, tuple[float, float]] = {
    "Kuala Lumpur": (3.139, 101.687),
    "Subang Jaya": (3.044, 101.581),
    "Petaling Jaya": (3.107, 101.606),
    "Georgetown": (5.414, 100.329),
}


class WeatherFetchError(RuntimeError):
    """the api did not give us something usable."""


Fetcher = Callable[[str], str]


def _http_get(url: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "pulseops/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise WeatherFetchError(f"open-meteo returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WeatherFetchError(f"could not reach open-meteo: {exc}") from exc


def build_url(latitude: float, longitude: float, past_days: int) -> str:
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": TIMEZONE,
            "past_days": max(0, min(past_days, 92)),
            "forecast_days": 1,
        }
    )
    return f"{ENDPOINT}?{query}"


def parse_response(city: str, payload: str, fetched_at: str) -> list[dict[str, Any]]:
    """turn one city's response into rows, or say clearly why it cannot.

    the arrays come back parallel rather than as objects, so they have to be
    zipped by index. if the api ever returns them at different lengths that is
    a real problem, and silently zipping to the shortest would hide it.
    """
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise WeatherFetchError(f"{city}: response was not json") from exc

    daily = body.get("daily")
    if not isinstance(daily, dict):
        raise WeatherFetchError(f"{city}: response had no daily block")

    days = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_sum") or []

    lengths = {len(days), len(highs), len(lows), len(rain)}
    if len(lengths) != 1:
        raise WeatherFetchError(
            f"{city}: open-meteo returned arrays of different lengths {lengths}, "
            "zipping these would invent readings"
        )

    return [
        {
            "city": city,
            "weather_date": day,
            "temp_max_c": highs[i],
            "temp_min_c": lows[i],
            "precipitation_mm": rain[i],
            "fetched_at": fetched_at,
            "source": "open-meteo",
        }
        for i, day in enumerate(days)
    ]


def fetch_weather(
    cities: dict[str, tuple[float, float]] | None = None,
    past_days: int = 7,
    fetcher: Fetcher = _http_get,
) -> list[dict[str, Any]]:
    """one row per city per day.

    a city that fails does not kill the run. losing Penang's weather is not a
    reason to lose the other three, and the failure is raised to the caller as
    a list rather than swallowed.
    """
    cities = cities or CITY_COORDS
    fetched_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    rows: list[dict[str, Any]] = []
    failures: list[str] = []

    for city, (lat, lon) in cities.items():
        try:
            rows.extend(parse_response(city, fetcher(build_url(lat, lon, past_days)), fetched_at))
        except WeatherFetchError as exc:
            failures.append(str(exc))

    if failures and not rows:
        raise WeatherFetchError("every city failed: " + "; ".join(failures))
    if failures:
        # partial success is still success, but it must be visible
        print(f"weather: {len(failures)} of {len(cities)} cities failed: {failures}")

    return rows


def validate_weather_rows(rows: list[dict[str, Any]]) -> list[str]:
    """sanity bounds, because a third party can send you anything.

    these are deliberately wide. the job is to catch a unit change or a null
    flood, not to second-guess the meteorologists.
    """
    problems: list[str] = []
    for row in rows:
        city, day = row.get("city"), row.get("weather_date")
        high, low = row.get("temp_max_c"), row.get("temp_min_c")
        rain = row.get("precipitation_mm")

        if not city or not day:
            problems.append(f"row missing city or date: {row}")
            continue
        try:
            date.fromisoformat(str(day))
        except ValueError:
            problems.append(f"{city} {day}: unparseable date")
        if high is None or not (-20 <= float(high) <= 60):
            problems.append(f"{city} {day}: temp_max {high} outside -20..60")
        if low is not None and high is not None and float(low) > float(high):
            problems.append(f"{city} {day}: min {low} above max {high}")
        if rain is not None and float(rain) < 0:
            problems.append(f"{city} {day}: negative rainfall {rain}")

    return problems
