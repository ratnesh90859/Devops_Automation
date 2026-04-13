# ──────────────────────────────────────────────────────────────
# Cloud Run v2 service: order-api
#
# Terraform provisions the initial service.
# After the first deploy, Bitbucket Pipelines owns the image tag.
# lifecycle.ignore_changes prevents Terraform from reverting CI/CD
# updates to the container image on subsequent `terraform apply`.
# ──────────────────────────────────────────────────────────────

locals {
  # Public hello-world placeholder used on first terraform apply.
  # Bitbucket Pipelines will replace this with the real image after first build.
  initial_image = "us-docker.pkg.dev/cloudrun/container/hello:latest"
}

resource "google_cloud_run_v2_service" "order_api" {
  name     = var.service_name
  location = var.region
  project  = var.project_id

  # Allow unauthenticated public traffic
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.order_api_sa.email

    # Cost optimisation: handle up to 80 concurrent requests per instance
    # before Cloud Run scales to the next instance.
    max_instance_request_concurrency = var.cloudrun_concurrency

    scaling {
      min_instance_count = var.cloudrun_min_instances
      max_instance_count = var.cloudrun_max_instances
    }

    timeout = "${var.cloudrun_timeout}s"

    containers {
      image = local.initial_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          memory = var.cloudrun_memory
          cpu    = var.cloudrun_cpu
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "LOAD_SIZE"
        value = "2000000"
      }

      env {
        name  = "SLEEP_SECONDS"
        value = "8"
      }

      liveness_probe {
        http_get {
          path = "/"
        }
        initial_delay_seconds = 10
        period_seconds        = 30
        failure_threshold     = 3
        timeout_seconds       = 5
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 10
        timeout_seconds       = 3
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository.infra_agent,
    google_service_account.order_api_sa,
  ]

  lifecycle {
    # CI/CD pipeline updates the image on every commit.
    # Terraform must not revert those changes.
    ignore_changes = [
      template[0].containers[0].image,
      template[0].revision,
      client,
      client_version,
    ]
  }
}

# IAM: allow unauthenticated invocations (public API)
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.order_api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
