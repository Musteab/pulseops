variable "project_id" {
  description = "gcp project id, no default on purpose so nobody applies this at the wrong project"
  type        = string
}

variable "region" {
  description = "region for everything regional"
  type        = string
  default     = "asia-southeast1"
}

variable "bq_location" {
  description = "bigquery dataset location. cannot be changed after creation, choose carefully"
  type        = string
  default     = "asia-southeast1"
}

variable "topic_name" {
  description = "pubsub topic the publisher writes to"
  type        = string
  default     = "pulseops-orders"
}

variable "max_delivery_attempts" {
  description = "how many times pubsub retries before a message goes to the dead letter topic"
  type        = number
  default     = 5
}

variable "table_expiration_days" {
  description = "auto-delete raw partitions after this long. keeps a demo project from growing forever"
  type        = number
  default     = 90
}
