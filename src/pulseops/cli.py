"""PulseOps command line entry point.

    python -m pulseops generate --events 5000 --fault-rate 0.05
    python -m pulseops validate --events data/raw/events.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .contracts import CURRENT_VERSION, validate_event
from .generator.catalog import menu_items_as_rows, outlets_as_rows
from .generator.generate import FAULT_TYPES, generate

DEFAULT_RAW = Path("data/raw")
DEFAULT_SEEDS = Path("data/seeds")


def _iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not YYYY-MM-DD") from exc


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def cmd_generate(args: argparse.Namespace) -> int:
    result = generate(
        n_events=args.events,
        seed=args.seed,
        start=args.start,
        end=args.end,
        fault_rate=args.fault_rate,
        fault_types=tuple(args.fault_types) if args.fault_types else FAULT_TYPES,
    )

    events_path = Path(args.out)
    manifest_path = events_path.with_name("faults_manifest.json")

    _write_jsonl(events_path, result.events)
    manifest_path.write_text(
        json.dumps(result.manifest(), indent=2) + "\n", encoding="utf-8"
    )

    _write_csv(DEFAULT_SEEDS / "dim_outlet_seed.csv", outlets_as_rows())
    _write_csv(DEFAULT_SEEDS / "dim_menu_item_seed.csv", menu_items_as_rows())

    by_layer = result.manifest()["fault_counts_by_layer"]
    print(f"run_id           {result.run_id}")
    print(f"seed             {result.seed}")
    print(f"window           {result.start} to {result.end}")
    print(f"events written   {len(result.events)}")
    print(f"faults injected  {len(result.faults)}  {by_layer}")
    print(f"events           {events_path}")
    print(f"manifest         {manifest_path}")
    print(f"seeds            {DEFAULT_SEEDS}/")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Run the contract validator over a file and report detection against truth.

    Only faults tagged `contract` are expected to be caught here. The ones
    tagged `warehouse` need reference data or cross-record context, so they are
    reported separately rather than counted as misses.
    """
    events_path = Path(args.events)
    manifest_path = events_path.with_name("faults_manifest.json")

    if not events_path.exists():
        print(f"no events at {events_path}, run generate first", file=sys.stderr)
        return 1

    rejected: dict[str, list[dict]] = {}
    total = 0
    with events_path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                rejected[f"line:{line_no}"] = [
                    {"code": "malformed_json", "path": "$", "detail": "not decodable"}
                ]
                continue
            outcome = validate_event(event)
            if not outcome.ok:
                key = event.get("event_id") or f"line:{line_no}"
                rejected[key] = [v.as_dict() for v in outcome.violations]

    print(f"contract version {CURRENT_VERSION}")
    print(f"events read      {total}")
    print(f"passed           {total - len(rejected)}")
    print(f"quarantined      {len(rejected)}")

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = [f for f in manifest["faults"] if f["detected_by"] == "contract"]
        deferred = [f for f in manifest["faults"] if f["detected_by"] != "contract"]

        caught = [f for f in expected if f["event_id"] in rejected]
        missed = [f for f in expected if f["event_id"] not in rejected]

        rate = (len(caught) / len(expected) * 100) if expected else 100.0
        print()
        print(f"ground truth     {manifest['fault_count']} faults injected")
        print(f"contract layer   {len(caught)}/{len(expected)} caught  ({rate:.1f}%)")
        print(f"warehouse layer  {len(deferred)} deferred to dbt tests")

        if missed:
            print()
            print("missed by the contract validator:")
            for fault_type, n in Counter(f["fault_type"] for f in missed).most_common():
                print(f"  {n:>4}  {fault_type}")

        if args.strict and missed:
            return 1

    if args.quarantine:
        out = Path(args.quarantine)
        _write_jsonl(
            out,
            [{"event_id": k, "violations": v} for k, v in rejected.items()],
        )
        print(f"\nquarantine       {out}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pulseops", description="Synthetic restaurant data platform toolkit"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="write synthetic order events plus ground truth")
    gen.add_argument("--events", type=int, default=5000, help="clean events to produce")
    gen.add_argument("--seed", type=int, default=42, help="rng seed, keeps runs reproducible")
    gen.add_argument("--start", type=_iso_date, default=None, help="window start YYYY-MM-DD")
    gen.add_argument("--end", type=_iso_date, default=None, help="window end YYYY-MM-DD")
    gen.add_argument("--fault-rate", type=float, default=0.0, help="share of events to corrupt")
    gen.add_argument(
        "--fault-types", nargs="*", choices=FAULT_TYPES, default=None,
        help="restrict to specific faults, default is all",
    )
    gen.add_argument("--out", default=str(DEFAULT_RAW / "events.jsonl"))
    gen.set_defaults(func=cmd_generate)

    val = sub.add_parser("validate", help="apply the contract and score detection")
    val.add_argument("--events", default=str(DEFAULT_RAW / "events.jsonl"))
    val.add_argument("--quarantine", default=None, help="write rejected records here")
    val.add_argument(
        "--strict", action="store_true",
        help="exit non-zero if any contract-layer fault slipped through",
    )
    val.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
