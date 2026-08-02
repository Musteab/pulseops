"""the daily batch: inventory snapshot, weather pull, then rebuild the marts.

runs on airflow in docker rather than cloud composer. composer bills roughly
three hundred dollars a month whether you use it or not, which is a strange
thing to pay so a demo can run one dag a day. the dag code is identical either
way, so moving it to composer later is a deploy, not a rewrite.

every task here is a thin wrapper over a function in src/pulseops/sources.
that is deliberate: the logic is unit tested without an airflow install, and
the dag only owns scheduling, retries and dependencies.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

from airflow.decorators import dag, task

# the package is mounted rather than installed so a code change does not need
# an image rebuild
sys.path.insert(0, "/opt/airflow/pulseops/src")

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "pulseops-muste")

DEFAULT_ARGS = {
    "owner": "pulseops",
    # two retries with a gap. open-meteo is a free public api and a single
    # timeout is not a reason to page anyone.
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "depends_on_past": False,
}


@dag(
    dag_id="pulseops_daily",
    description="daily inventory snapshot and weather pull, then rebuild the marts",
    schedule="0 2 * * *",  # 02:00, after the trading day has closed everywhere
    start_date=datetime(2026, 7, 1),
    catchup=False,  # backfilling weather would hammer a free api for no benefit
    max_active_runs=1,  # two runs writing the same partition is a race
    default_args=DEFAULT_ARGS,
    tags=["pulseops", "batch"],
)
def pulseops_daily():
    @task
    def load_inventory_snapshot(days: int = 1) -> dict[str, int]:
        """generate today's stock levels and replace today's partition."""
        from pulseops.sources.inventory import generate_inventory, validate_inventory_rows
        from pulseops.sources.load import load_inventory, today

        rows = generate_inventory(snapshot_date=today(), days=days)

        problems = validate_inventory_rows(rows)
        if problems:
            # fail before writing. a batch that is wrong on arrival should not
            # reach raw, because raw is the thing everything else trusts.
            raise ValueError(f"inventory failed validation: {problems[:5]}")

        written = load_inventory(PROJECT_ID, rows)
        print(f"inventory: {sum(written.values())} rows across {len(written)} partitions")
        return written

    @task
    def load_weather_readings(past_days: int = 3) -> dict[str, int]:
        """pull the last few days from open-meteo and replace those partitions.

        re-pulling days we already have is intentional. weather providers
        restate recent observations, so yesterday's number can change, and
        replacing the partition picks that up for free.
        """
        from pulseops.sources.load import load_weather
        from pulseops.sources.weather import fetch_weather, validate_weather_rows

        rows = fetch_weather(past_days=past_days)
        if not rows:
            raise ValueError("open-meteo returned nothing for any city")

        problems = validate_weather_rows(rows)
        if problems:
            raise ValueError(f"weather failed validation: {problems[:5]}")

        written = load_weather(PROJECT_ID, rows)
        print(f"weather: {sum(written.values())} rows across {len(written)} partitions")
        return written

    @task
    def rebuild_marts(inventory: dict, weather: dict) -> str:
        """run dbt once both sources have landed.

        takes both upstream results as arguments purely to declare the
        dependency. dbt does not read them, but airflow will not start this
        until both have actually succeeded.
        """
        import subprocess

        result = subprocess.run(
            [
                "/opt/airflow/dbt-venv/bin/dbt", "build",
                "--project-dir", "/opt/airflow/pulseops/dbt",
                "--profiles-dir", "/opt/airflow/pulseops/dbt",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout[-4000:])
        if result.returncode != 0:
            raise RuntimeError(f"dbt build failed:\n{result.stderr[-2000:]}")
        return "marts rebuilt"

    rebuild_marts(load_inventory_snapshot(), load_weather_readings())


pulseops_daily()
