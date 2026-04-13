output "cloud_run_service_url" {
  description = "Public URL of the deployed Cloud Run service (order-api)."
  value       = google_cloud_run_v2_service.order_api.uri
}

output "artifact_registry_url" {
  description = "Docker registry URL for pushing images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.registry_name}"
}

output "order_api_image_path" {
  description = "Full image path to use in CI/CD (append :<tag>)."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.registry_name}/${var.service_name}"
}

output "app_service_account_email" {
  description = "Service account email that runs the Cloud Run service."
  value       = google_service_account.order_api_sa.email
}

output "agent_service_account_email" {
  description = "Service account email for the Infra AI agent."
  value       = google_service_account.infra_agent_sa.email
}

output "agent_service_account_key_instruction" {
  description = "Command to create and download the agent key."
  value       = "gcloud iam service-accounts keys create gcp-key.json --iam-account=${google_service_account.infra_agent_sa.email} --project=${var.project_id}"
}

output "state_bucket_name" {
  description = "GCS bucket that stores Terraform state."
  value       = google_storage_bucket.tf_state.name
}

output "project_number" {
  description = "GCP project number."
  value       = data.google_project.project.number
}

output "firestore_database" {
  description = "Firestore database name used for incident storage."
  value       = google_firestore_database.default.name
}
