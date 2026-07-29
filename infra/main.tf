# everything the pipeline needs, in one apply.
#
# nothing here costs money at the volumes this project runs at. pubsub gives you
# 10GB a month free and bigquery 10GB of storage plus 1TB of queries, and a few
# thousand json events is megabytes. no dataflow and no composer anywhere in
# this file, which is deliberate: those two are what turn a demo project into a
# real bill.

# ---------------------------------------------------------------------------
# pubsub
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "orders" {
  name = var.topic_name

  # a schema is deliberately NOT attached here. pubsub would reject malformed
  # events at the edge, which sounds good until you realise it means your
  # quarantine table stays empty forever and you can never study a real fault.
  # the contract is enforced by the subscriber, where we can record why.

  message_retention_duration = "604800s" # 7 days, so replay is possible after a bad weekend
}

# where messages go after we have failed to process them too many times.
# without this a poison message redelivers forever and blocks nothing but your sanity.
resource "google_pubsub_topic" "orders_dead_letter" {
  name = "${var.topic_name}-dead-letter"
}

resource "google_pubsub_subscription" "orders_ingest" {
  name  = "${var.topic_name}-ingest"
  topic = google_pubsub_topic.orders.id

  # pull, not push. a push subscription needs a public https endpoint, which
  # means cloud run, which means more moving parts than this needs today.
  ack_deadline_seconds = 30

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.orders_dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  expiration_policy {
    ttl = "" # never expire, otherwise an idle week deletes the subscription
  }
}

# ---------------------------------------------------------------------------
# bigquery
# ---------------------------------------------------------------------------

resource "google_bigquery_dataset" "raw" {
  dataset_id  = "pulseops_raw"
  location    = var.bq_location
  description = "append only, exactly as received. never edited, never patched in place."
}

resource "google_bigquery_dataset" "quarantine" {
  dataset_id  = "pulseops_quarantine"
  location    = var.bq_location
  description = "records that failed the contract, kept with their violations so they can be replayed"
}

resource "google_bigquery_dataset" "staging" {
  dataset_id  = "pulseops_staging"
  location    = var.bq_location
  description = "typed, deduplicated, conformed. dbt owns everything in here."
}

resource "google_bigquery_dataset" "mart" {
  dataset_id  = "pulseops_mart"
  location    = var.bq_location
  description = "the dimensional model. the only layer dashboards and the copilot may read."
}

resource "google_bigquery_table" "orders_raw" {
  dataset_id          = google_bigquery_dataset.raw.dataset_id
  table_id            = "orders_raw"
  schema              = file("${path.module}/schemas/orders_raw.json")
  deletion_protection = false

  # forces every query to filter by date. it is mildly annoying and it is the
  # single cheapest guard against someone full-scanning the table by accident.
  require_partition_filter = true

  # partition on ingest date because that is what every backfill and every
  # "what landed yesterday" query filters on. clustering on event_id makes the
  # dedupe lookups in staging cheap.
  time_partitioning {
    type          = "DAY"
    field         = "ingest_ts"
    expiration_ms = var.table_expiration_days * 24 * 60 * 60 * 1000
  }

  clustering = ["event_id"]
}

resource "google_bigquery_table" "orders_quarantine" {
  dataset_id          = google_bigquery_dataset.quarantine.dataset_id
  table_id            = "orders_quarantine"
  schema              = file("${path.module}/schemas/orders_quarantine.json")
  deletion_protection = false

  # left off here, unlike raw. quarantine stays small and you often want to ask
  # "has this ever happened before" without knowing which day to look at.
  require_partition_filter = false

  time_partitioning {
    type  = "DAY"
    field = "quarantined_ts"
    # no expiration on purpose. raw data ages out, but the record of what broke
    # and when is exactly the thing you want six months later.
  }

  clustering = ["schema_version", "event_id"]
}
