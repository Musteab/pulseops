"""the cli. everything you can do to this pipeline from a terminal.

    python -m pulseops generate --events 5000 --fault-rate 0.05
    python -m pulseops validate --events data/raw/events.jsonl
    python -m pulseops publish --sink file://data/raw/published.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .contracts import CURRENT_VERSION, validate_event
from .generator.catalog import menu_items_as_rows, outlets_as_rows
from .generator.generate import FAULT_TYPES, generate
from .ingest.publish import publish_events, read_events
from .ingest.sinks import sink_from_uri

DEFAULT_RAW = Path("data/raw")
# seeds go straight into the dbt project. they are deterministic reference data
# derived from catalog.py, so generating them and having dbt load them keeps one
# source of truth instead of a csv someone edits by hand and forgets to sync
DEFAULT_SEEDS = Path("dbt/seeds")
DEFAULT_WAREHOUSE = Path("data/warehouse")


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


def cmd_publish(args: argparse.Namespace) -> int:
    events_path = Path(args.events)
    if not events_path.exists():
        print(f"no events at {events_path}, run generate first", file=sys.stderr)
        return 1

    # sink uri can come from the env so the same command works locally and
    # against the real project without anyone editing a makefile
    uri = args.sink or os.environ.get("PULSEOPS_SINK") or f"file://{DEFAULT_RAW}/published.jsonl"

    print(f"sink             {uri}")
    with sink_from_uri(uri) as sink:
        stats = publish_events(
            read_events(events_path), sink, limit=args.limit, progress_every=args.progress_every
        )

    numbers = stats.as_dict()
    print(f"published        {numbers['published']}")
    if numbers["failed"]:
        print(f"failed           {numbers['failed']}")
    print(f"elapsed          {numbers['elapsed_s']}s")
    print(f"throughput       {numbers['throughput_per_s']}/s")
    lat = numbers["latency_ms"]
    print(
        f"latency ms       p50 {lat['p50']}  p95 {lat['p95']}  "
        f"p99 {lat['p99']}  max {lat['max']}"
    )

    if args.stats_out:
        out = Path(args.stats_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")
        print(f"stats            {out}")

    return 1 if numbers["failed"] else 0


def cmd_subscribe(args: argparse.Namespace) -> int:
    from .ingest.stores import store_from_uri
    from .ingest.subscribe import pull_and_route

    store_uri = args.store or os.environ.get("PULSEOPS_STORE") or f"file://{DEFAULT_WAREHOUSE}"
    project = args.project or os.environ.get("GCP_PROJECT_ID")
    if not project:
        print("need --project or $GCP_PROJECT_ID", file=sys.stderr)
        return 1

    print(f"subscription     {project}/{args.subscription}")
    print(f"store            {store_uri}")

    with store_from_uri(store_uri) as store:
        counts = pull_and_route(
            project_id=project,
            subscription_id=args.subscription,
            store=store,
            max_messages=args.max_messages,
        )

    numbers = counts.as_dict()
    print(f"routed           {numbers['total']}")
    print(f"  raw            {numbers['raw']}")
    print(f"  quarantined    {numbers['quarantine']}")

    if numbers["violation_codes"]:
        print("\nwhy things were rejected:")
        for code, n in sorted(numbers["violation_codes"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {code}")

    if args.stats_out:
        out = Path(args.stats_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")
        print(f"\nstats            {out}")

    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .ingest.replay import repair_rules, replay_quarantine
    from .ingest.stores import store_from_uri

    store_uri = args.store or os.environ.get("PULSEOPS_STORE") or f"file://{DEFAULT_WAREHOUSE}"

    if args.list_rules:
        print("repairs this pipeline knows how to make:\n")
        for name, describes in repair_rules():
            print(f"  {name:<20} {describes}")
        print("\neverything else is refused. repairing format is fine, inventing")
        print("values that were never sent is not.")
        return 0

    print(f"store            {store_uri}")
    if args.dry_run:
        print("mode             dry run, nothing will be written")

    with store_from_uri(store_uri) as store:
        counts = replay_quarantine(store, limit=args.limit, dry_run=args.dry_run)

    numbers = counts.as_dict()
    print(f"attempted        {numbers['attempted']}")
    print(f"  repaired       {numbers['repaired']}")
    print(f"  unrepairable   {numbers['unrepairable']}")
    if numbers["still_invalid"]:
        print(f"  still invalid  {numbers['still_invalid']}")

    if numbers["rules_fired"]:
        print("\nrepairs applied:")
        for rule, n in sorted(numbers["rules_fired"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:>5}  {rule}")

    if args.stats_out:
        out = Path(args.stats_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")
        print(f"\nstats            {out}")

    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from .copilot.agent import Copilot

    project = args.project or os.environ.get("GCP_PROJECT_ID")
    if not project:
        print("need --project or $GCP_PROJECT_ID", file=sys.stderr)
        return 1

    turn = Copilot(project_id=project, model=args.model).ask(args.question)

    if turn.error:
        print(f"error: {turn.error}", file=sys.stderr)
        return 1

    print(turn.answer)
    if turn.tool_calls:
        print(f"\ntools    {', '.join(turn.tool_calls)}")
    if turn.sources:
        print(f"sources  {', '.join(sorted(set(turn.sources)))}")
    if turn.tools_refused:
        print(f"refused  {', '.join(turn.tools_refused)}")
    return 0


def _eval_repeated(args: argparse.Namespace, project: str, cases) -> int:
    """run the suite several times and report the spread rather than one number."""
    from .copilot.agent import Copilot
    from .copilot.evaluate import run_eval_repeated

    print(f"running {len(cases)} cases x {args.repeat} against {args.model}\n")

    repeated = run_eval_repeated(
        lambda: Copilot(project_id=project, model=args.model),
        cases,
        runs=args.repeat,
        workers=args.workers,
        on_run=lambda n, card: print(f"  run {n}: {card.passed}/{card.total}"),
    )

    numbers = repeated.as_dict()
    print()
    print(f"runs             {numbers['runs']}")
    print(f"passed           {numbers['passed_per_run']} of {numbers['total_per_run']}")
    print(f"mean             {numbers['mean']}/{numbers['total_per_run']}")
    print(f"worst            {numbers['worst']}/{numbers['total_per_run']}")

    print()
    if numbers["safety_refused_every_run"]:
        print(f"safety           refused in all {numbers['runs']} runs, 0 unsafe actions")
    else:
        print(f"safety           {numbers['unsafe_actions_total']} UNSAFE ACTIONS")

    if numbers["flaky_cases"]:
        print("\nflaky cases, pass rate across runs:")
        for case_id, rate in numbers["flaky_cases"].items():
            print(f"  {rate * 100:>5.0f}%  {case_id}")

    if args.stats_out:
        out = Path(args.stats_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")
        print(f"\nstats            {out}")

    if repeated.unsafe_actions:
        print("\nUNSAFE ACTION TAKEN, this is a hard failure", file=sys.stderr)
        return 2
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .copilot.agent import Copilot
    from .copilot.evalset import CASES
    from .copilot.evaluate import run_eval

    project = args.project or os.environ.get("GCP_PROJECT_ID")
    if not project:
        print("need --project or $GCP_PROJECT_ID", file=sys.stderr)
        return 1

    cases = CASES
    if args.category:
        cases = tuple(c for c in CASES if c.category == args.category)
    if not cases:
        print(f"no cases in category {args.category!r}", file=sys.stderr)
        return 1

    if args.repeat > 1:
        return _eval_repeated(args, project, cases)

    print(f"running {len(cases)} cases against {args.model}\n")

    card = run_eval(
        lambda: Copilot(project_id=project, model=args.model),
        cases,
        workers=args.workers,
        on_result=lambda r: print(f"  {'pass' if r.passed else 'FAIL'}  {r.case_id}"),
    )

    numbers = card.as_dict()
    print()
    print(f"passed           {numbers['passed']}/{numbers['total']}")
    print(f"tool selection   {numbers['tool_selection_rate'] * 100:.0f}%")
    print(f"answer accuracy  {numbers['answer_accuracy_rate'] * 100:.0f}%")
    print()
    for category, counts in numbers["by_category"].items():
        print(f"  {category:<12} {counts['passed']}/{counts['total']}")

    safety = numbers["safety"]
    print()
    print(f"safety           {safety['refused']}/{safety['cases']} refused")

    for result in card.results:
        if not result.passed:
            print(f"\n  FAIL {result.case_id}")
            for failure in result.failures:
                print(f"       {failure}")

    if args.stats_out:
        out = Path(args.stats_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(numbers, indent=2) + "\n", encoding="utf-8")
        print(f"\nstats            {out}")

    # an unsafe action fails the command regardless of the headline score
    if card.unsafe_actions:
        print("\nUNSAFE ACTION TAKEN, this is a hard failure", file=sys.stderr)
        return 2
    return 0 if card.passed == card.total else 1


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import build_dashboard

    project = args.project or os.environ.get("GCP_PROJECT_ID")
    if not project:
        print("need --project or $GCP_PROJECT_ID", file=sys.stderr)
        return 1

    out = build_dashboard(project, args.out)
    size = out.stat().st_size / 1024
    print(f"dashboard        {out}  ({size:.0f} KB, self-contained)")
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

    pub = sub.add_parser("publish", help="push events at a sink and time it")
    pub.add_argument("--events", default=str(DEFAULT_RAW / "events.jsonl"))
    pub.add_argument(
        "--sink", default=None,
        help="file://path or pubsub://project/topic, falls back to $PULSEOPS_SINK",
    )
    pub.add_argument("--limit", type=int, default=None, help="stop after this many events")
    pub.add_argument("--stats-out", default=None, help="write the run stats as json")
    pub.add_argument(
        "--progress-every", type=int, default=0, help="print progress every n events",
    )
    pub.set_defaults(func=cmd_publish)

    subs = sub.add_parser("subscribe", help="drain the queue, enforce the contract, route")
    subs.add_argument("--project", default=None, help="gcp project, falls back to $GCP_PROJECT_ID")
    subs.add_argument("--subscription", default="pulseops-orders-ingest")
    subs.add_argument(
        "--store", default=None,
        help="file://directory or bq://project, falls back to $PULSEOPS_STORE",
    )
    subs.add_argument("--max-messages", type=int, default=100_000)
    subs.add_argument("--stats-out", default=None, help="write the routing counts as json")
    subs.set_defaults(func=cmd_subscribe)

    rep = sub.add_parser("replay", help="repair what can be repaired and re-send it")
    rep.add_argument(
        "--store", default=None,
        help="file://directory or bq://project, falls back to $PULSEOPS_STORE",
    )
    rep.add_argument("--limit", type=int, default=10_000)
    rep.add_argument(
        "--dry-run", action="store_true",
        help="report what would happen without writing anything",
    )
    rep.add_argument(
        "--list-rules", action="store_true", help="show the repairs and exit",
    )
    rep.add_argument("--stats-out", default=None)
    rep.set_defaults(func=cmd_replay)

    ask = sub.add_parser("ask", help="put a question to the copilot")
    ask.add_argument("question")
    ask.add_argument("--project", default=None)
    ask.add_argument("--model", default="gemini-2.5-pro")
    ask.set_defaults(func=cmd_ask)

    ev = sub.add_parser("eval", help="score the copilot against the eval cases")
    ev.add_argument("--project", default=None)
    ev.add_argument("--model", default="gemini-2.5-pro")
    ev.add_argument(
        "--category", default=None,
        choices=["analytics", "reliability", "safety", "humility"],
    )
    ev.add_argument("--workers", type=int, default=3)
    ev.add_argument(
        "--repeat", type=int, default=1,
        help="run the suite N times and report the spread, llm evals are not deterministic",
    )
    ev.add_argument("--stats-out", default=None)
    ev.set_defaults(func=cmd_eval)

    dash = sub.add_parser("dashboard", help="build a self-contained html dashboard")
    dash.add_argument("--project", default=None)
    dash.add_argument("--out", default="dashboard.html")
    dash.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
