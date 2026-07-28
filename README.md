# PulseOps

**A data platform that knows when it is lying to you.**

Revenue for one outlet drops 40 percent overnight. Did sales actually fall, or did the pipeline break? PulseOps is built to answer that question, and to prove the answer with numbers rather than vibes.

It is a synthetic restaurant order platform with a three-layer BigQuery warehouse, contract-enforced ingestion, deliberate fault injection, and (in progress) an agentic copilot that investigates anomalies with read-only tools.

[![ci](https://github.com/Musteab/pulseops/actions/workflows/ci.yml/badge.svg)](https://github.com/Musteab/pulseops/actions/workflows/ci.yml)

---

## Why this exists

Most data-quality projects assert that they work. This one is scored against ground truth.

The generator deliberately corrupts a known share of events and writes a manifest recording exactly which event ids were broken and how. Every quality number below is measured against that manifest, on a fixed seed, in CI. If a check regresses, the build fails.

```
$ make demo

run_id           run-42-5000
seed             42
window           2026-06-30 to 2026-07-29
events written   5022
faults injected  250  {'contract': 185, 'warehouse': 65}

contract version 1.0.0
events read      5022
passed           4837
quarantined      185

ground truth     250 faults injected
contract layer   185/185 caught  (100.0%)
warehouse layer  65 deferred to dbt tests
```

That last split is the honest part. The ingest-time contract cannot catch a duplicate delivery or an orphaned foreign key, because neither is visible from a single record. Those are tagged `warehouse` and handed to the dbt test layer instead of being quietly counted as a win.

## Quickstart

Needs Python 3.11 or newer. No cloud account, no credentials, no Docker.

```bash
git clone https://github.com/Musteab/pulseops.git
cd pulseops
make setup
make demo
```

Roughly 30 seconds from clone to a scored quality report.

## Architecture

```mermaid
flowchart LR
  subgraph sources[Sources]
    A[Order events<br/>synthetic stream]
    B[Inventory CSV<br/>daily batch]
    C[Weather API<br/>daily pull]
  end

  A --> PS[Pub/Sub]
  PS --> ING[Contract validator]
  B --> AF[Airflow DAG]
  C --> AF
  AF --> ING

  ING -->|valid| RAW[(raw)]
  ING -->|rejected| QT[(quarantine)]
  QT -.replay after fix.-> ING

  RAW --> STG[(staging<br/>dbt)]
  STG --> MART[(marts<br/>star schema)]
  MART --> DASH[Looker Studio]

  QT --> COP
  MART --> COP
  COP[Copilot<br/>read-only tools] --> ANS[Answer with lineage]
```

**Three layers, one rule each.** `raw` is append-only and never edited. `staging` is where types are cast, keys are deduplicated, and dbt tests run. `marts` is the dimensional model that dashboards and the copilot are allowed to touch.

**Rejected records are not dropped.** They land in quarantine with the full list of contract violations, which means a fault can be diagnosed and the records replayed once the producer is fixed. Silently discarding bad rows is how revenue goes missing without anyone noticing.

## The data contract

`src/pulseops/contracts.py` is the agreement between producer and consumer, and it is versioned. It is deliberately boring code, but it is the spine of the project: the generator emits against it, ingestion enforces it, and the tests prove the two agree.

It returns every violation for a record rather than failing on the first, so quarantine rows carry a complete diagnosis:

```json
{
  "event_id": "6f9254fd-12e1-44ce-b327-6dbb5605dfb3",
  "violations": [
    {"code": "missing_field", "path": "outlet_id", "detail": "required field outlet_id absent"}
  ]
}
```

## Injected faults

Twelve fault types, each tagged with the layer expected to catch it.

| Fault | Caught by | What it simulates |
|---|---|---|
| `schema_drift` | contract | Producer renames a field and bumps its version without telling anyone |
| `unknown_channel` | contract | A new enum value appears that nobody agreed to |
| `missing_outlet_id` | contract | Required field dropped upstream |
| `null_order_total` | contract | Nulls where the contract forbids them |
| `negative_unit_price` | contract | Sign error in the POS |
| `negative_qty` | contract | Refund written as a negative line |
| `unparseable_timestamp` | contract | Locale-formatted date instead of ISO-8601 |
| `line_total_mismatch` | contract | Arithmetic that does not reconcile |
| `empty_lines` | contract | Order with no items |
| `duplicate_event` | warehouse | At-least-once delivery replaying an event id |
| `orphan_menu_item` | warehouse | Foreign key with no matching dimension row |
| `late_arrival` | warehouse | Valid record landing days into a closed window |

`schema_drift` is the one behind the headline demo. It renames `order_total_myr` to `total_amount`, so affected records fail the contract, land in quarantine, and revenue for that outlet appears to collapse. The pipeline is broken, the business is fine, and the copilot's job is to tell those apart.

## Reproducibility

Same seed in, byte-identical events out. Every reported number names the seed that produced it, and CI regenerates the reference dataset on every push and fails if contract detection drops below 100 percent.

```bash
python -m pulseops generate --events 5000 --seed 42 --fault-rate 0.05
python -m pulseops validate --strict
```

Restrict to a single fault type to study one failure mode in isolation:

```bash
python -m pulseops generate --events 500 --fault-rate 1.0 --fault-types schema_drift
```

## Status

Built and tested:

- [x] Versioned data contract with full violation reporting
- [x] Deterministic event generator with shaped traffic (hourly peaks, outlet weighting, payment failure rates)
- [x] Twelve injected fault types with a ground-truth manifest
- [x] Contract validation, quarantine output, and scored detection
- [x] 24 tests, lint, and a CI job that enforces the headline number

Roadmap, in build order:

- [ ] Pub/Sub streaming ingestion into the BigQuery raw layer
- [ ] dbt staging models, star schema (`fct_order_line`, `dim_outlet`, `dim_menu_item`, `dim_date`), and warehouse-layer tests
- [ ] Quarantine replay path with idempotency guarantees
- [ ] Airflow DAG for the batch and API sources
- [ ] Terraform for all GCP resources
- [ ] Looker Studio dashboard for revenue and pipeline health
- [ ] Data-reliability copilot with allowlisted read-only tools
- [ ] Agent evaluation suite scoring tool selection, answer accuracy, and refusal of unsafe writes

Nothing is claimed here that is not in the repo. Items above the line run today; items below it do not exist yet.

## Layout

```
src/pulseops/
  contracts.py          the versioned agreement, and the validator
  generator/
    catalog.py          outlets and menu items, also emitted as dbt seeds
    generate.py         deterministic event generation
    faults.py           the twelve injected faults and their ground truth
  cli.py                generate and validate commands
tests/                  24 tests, including one per fault type
```

## Data

All data is synthetic and generated locally. No real customer, order, or restaurant information is used anywhere in this project.

## Licence

MIT
