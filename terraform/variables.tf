variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region for all resources."
  type        = string
  default     = "asia-south1"
}

variable "service_name" {
  description = "Name of the Cloud Run service being monitored."
  type        = string
  default     = "order-api"
}

variable "registry_name" {
  description = "Artifact Registry repository name."
  type        = string
  default     = "infra-agent"
}

variable "cloudrun_memory" {
  description = "Memory limit for the Cloud Run service (e.g. 256Mi, 512Mi)."
  type        = string
  default     = "256Mi"
}

variable "cloudrun_cpu" {
  description = "CPU limit for the Cloud Run service (e.g. 1, 2)."
  type        = string
  default     = "1"
}

variable "cloudrun_min_instances" {
  description = "Minimum number of Cloud Run instances."
  type        = number
  default     = 0
}

variable "cloudrun_max_instances" {
  description = "Maximum number of Cloud Run instances."
  type        = number
  default     = 3
}

variable "cloudrun_concurrency" {
  description = "Max concurrent requests per Cloud Run instance before a new one is started. Higher = fewer instances = lower cost."
  type        = number
  default     = 80
}

variable "cloudrun_timeout" {
  description = "Request timeout in seconds for the Cloud Run service."
  type        = number
  default     = 30
}

variable "bitbucket_service_account_email" {
  description = "Email of the Bitbucket Pipelines service account that will push images and deploy."
  type        = string
  default     = ""
}

variable "agent_service_account_display_name" {
  description = "Display name for the Infra AI agent service account."
  type        = string
  default     = "Infra AI Debugger Agent"
}

variable "state_bucket_location" {
  description = "Location for the GCS Terraform state bucket."
  type        = string
  default     = "ASIA-SOUTH1"
}
