output "sink_uri" {
  description = "paste this into PULSEOPS_SINK and the publisher goes to the cloud"
  value       = "pubsub://${var.project_id}/${google_pubsub_topic.orders.name}"
}

output "subscription" {
  description = "what the subscriber pulls from"
  value       = google_pubsub_subscription.orders_ingest.name
}

output "dead_letter_topic" {
  description = "where messages land after too many failed deliveries"
  value       = google_pubsub_topic.orders_dead_letter.name
}

output "raw_table" {
  value = "${var.project_id}.${google_bigquery_dataset.raw.dataset_id}.${google_bigquery_table.orders_raw.table_id}"
}

output "quarantine_table" {
  value = "${var.project_id}.${google_bigquery_dataset.quarantine.dataset_id}.${google_bigquery_table.orders_quarantine.table_id}"
}
