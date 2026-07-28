# Architecture

Design decisions and the reasoning behind them. The README says what PulseOps does; this says why it is built this way.

## The question the system exists to answer

> Outlet OUT-KL-001's revenue fell 40 percent yesterday. Is that real?

Answering it needs three things most pipelines do not keep: a record of what was rejected and why, lineage from a number back to the tables that produced it, and freshness metadata for every source. The layering below exists to preserve all three.

## Layers

| Layer | Contents | Rule |
|---|---|---|
| `raw` | Events exactly as received, plus ingest metadata | Append-only. Never edited, never deleted. Replayable. |
| `quarantine` | Records that failed the contract, with every violation | Kept, not dropped. Replayable after the producer is fixed. |
| `staging` | Typed, deduplicated, conformed | One model per source. dbt tests live here. |
| `mart` | Dimensional model | The only layer dashboards and the copilot may query. |

Raw is append-only because a pipeline that mutates its own history cannot be audited. When a bug is found in a transformation, the fix is to rebuild downstream from raw, not to patch rows in place.

## Why a contract instead of schema-on-read

BigQuery will happily accept an event whose `order_total_myr` was renamed to `total_amount`. The column simply arrives null, the daily revenue mart quietly under-reports, and nobody finds out until someone questions a dashboard weeks later.

An explicit versioned contract turns that silent corruption into a loud, dated, countable event: 185 records rejected at 14:32, all with the same violation code, all from one producer. That is the difference between a data incident you can investigate and one you never learn about.

The contract accepts exactly one schema version. A producer emitting `2.0.0` is rejected until a consumer is written that declares support for it. Version negotiation is a feature, not an inconvenience.

## Why faults carry a `detected_by` tag

Each injected fault declares which layer should catch it:

- **contract** faults are visible from a single record in isolation, so ingest rejects them.
- **warehouse** faults need something ingest does not have. A duplicate needs the other records. An orphan foreign key needs the dimension table. A late arrival needs to know when the window closed.

Without this split, "detection rate" is a meaningless number. Tagging makes the claim precise: 100 percent of contract-layer faults are caught at ingest, and the remaining faults are the dbt layer's responsibility. Two tests enforce the tags in both directions, so a fault cannot be quietly relabelled to make the headline figure look better.

## Determinism

Every quality claim names a seed. `generate(seed=42)` produces byte-identical output on any machine, including which events get corrupted and with what.

This matters twice. First, a metric nobody can reproduce is an anecdote. Second, the agent evaluation suite needs a fixed world: scoring an agent against data that changes each run measures noise.

The one place wall-clock time could leak in is `ingest_ts` for records whose own timestamp is unparseable. That falls back to an anchor derived from the generation window rather than `now()`, which keeps reruns identical.

## Dimensional model (planned)

```
fct_order_line          grain: one row per order line
  order_id, line_seq, outlet_key, menu_item_key, date_key,
  qty, unit_price_myr, line_total_myr, payment_status

fct_inventory_snapshot  grain: one row per outlet per item per day
dim_outlet              SCD type 2
dim_menu_item           SCD type 2, prices change
dim_date                standard calendar, Malaysian public holidays
```

Order-line grain rather than order grain, because the questions worth asking (which item drove the drop, which category carries margin) are per-item. Aggregating up to order level is cheap; splitting back down is impossible.

`fct_order_line` will be partitioned on `date_key` and clustered on `outlet_key`. Almost every query filters by date and groups by outlet, and the partition-versus-full-scan cost difference is worth measuring and publishing.

## Idempotency

Ingestion keys on `event_id`. Re-running the same batch must not change any number. This is what makes quarantine replay safe: fix the producer, replay the quarantined records, and the totals converge to the correct value instead of double counting.

## The copilot (planned)

Two agents behind a supervisor:

- an analytics agent that answers business questions from the marts
- a data-reliability agent that answers questions about the pipeline itself, reading quarantine counts, freshness, and test results

**Every tool is read-only and allowlisted.** No tool can issue DML or DDL. The evaluation suite includes cases that explicitly ask the agent to delete or update data, and refusing all of them is a scored requirement, not a nice-to-have. An agent with write access to a warehouse is a liability regardless of how well it answers questions.

Answers cite their sources: which tables were read, which query jobs ran, and how fresh the data was. An unsourced answer from an agent is not usable in an incident.

## Cost

Built to run on the GCP free tier where possible. Pub/Sub writes to BigQuery through a native subscription rather than a Dataflow job, and Airflow runs locally in Docker rather than on Cloud Composer, which has a substantial always-on cost. Where a component runs locally, the README says so rather than implying a managed deployment.
