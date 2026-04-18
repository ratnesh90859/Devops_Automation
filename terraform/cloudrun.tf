# ──────────────────────────────────────────────────────────────
# Cloud Run v2 service: order-api
#
# Terraform provisions the initial service.
# After the first deploy, Bitbucket Pipelines owns the image tag.
# lifecycle.ignore_changes prevents Terraform from reverting CI/CD
# updates to the container image on subsequent `terraform apply`.
# ──────────────────────────────────────────────────────────────

locals {
  # Use the var.container_image which PATH A dynamically resolves from the
  # currently running Cloud Run revision before terraform apply.
  initial_image = var.container_image
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

      # ── Threshold Monitor ────────────────────────────────────────────
      # The app counts every request from zero. When total_requests hits
      # THRESHOLD_REQUESTS (default 100) or total_errors hits
      # THRESHOLD_ERRORS (default 10) it POSTs an alert to the AI agent.
      env {
        name  = "AGENT_WEBHOOK_URL"
        value = var.agent_webhook_url
      }

      env {
        name  = "WEBHOOK_SECRET"
        value = var.webhook_secret
      }

      env {
        name  = "THRESHOLD_REQUESTS"
        value = tostring(var.threshold_requests)
      }

      env {
        name  = "THRESHOLD_ERRORS"
        value = tostring(var.threshold_errors)
      }

      env {
        name  = "THRESHOLD_COOLDOWN_SECS"
        value = tostring(var.threshold_cooldown_secs)
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
    # template[0].containers[0].image is intentionally NOT ignored:
    # PATH A resolves the current running image into var.container_image
    # before every terraform apply, so Terraform always deploys the correct image.
    ignore_changes = [
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
