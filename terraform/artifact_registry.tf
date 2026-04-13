# Docker repository in Artifact Registry.
# CI/CD pushes images here; Cloud Run pulls from here.

resource "google_artifact_registry_repository" "infra_agent" {
  provider = google-beta

  project       = var.project_id
  location      = var.region
  repository_id = var.registry_name
  description   = "Docker images for the Infra AI Debugger project"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-last-10"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  cleanup_policies {
    id     = "delete-older-than-30-days"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.apis]
}

# GCS bucket for Terraform remote state
# Create this bucket manually before the first `terraform init -backend-config`
# OR run the bootstrap target below once with local state, then migrate.
resource "google_storage_bucket" "tf_state" {
  name          = "${var.project_id}-terraform-state"
  location      = var.state_bucket_location
  project       = var.project_id
  force_destroy = false

  versioning {
    enabled = true
  }

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# Grant the agent SA read access to the state bucket (read-only for auditing)
resource "google_storage_bucket_iam_member" "agent_state_reader" {
  bucket = google_storage_bucket.tf_state.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.infra_agent_sa.email}"
}
