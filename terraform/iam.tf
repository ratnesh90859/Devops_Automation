# ──────────────────────────────────────────────────────────────
# Service Account: order-api (runs inside Cloud Run)
# Minimal permissions — only needs to write its own logs.
# ─────────────────────────────────────────────────────────────
resource "google_service_account" "order_api_sa" {
  account_id   = "${var.service_name}-sa"
  display_name = "Order API Cloud Run SA"
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "order_api_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.order_api_sa.email}"
}

resource "google_project_iam_member" "order_api_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.order_api_sa.email}"
}

# ──────────────────────────────────────────────────────────────
# Service Account: infra-agent
# Used by the FastAPI agent running in Docker / Cloud Run.
# Needs: read logs, manage Cloud Run, read Artifact Registry.
# ──────────────────────────────────────────────────────────────
resource "google_service_account" "infra_agent_sa" {
  account_id   = "infra-agent-sa"
  display_name = var.agent_service_account_display_name
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

# Read Cloud Logging entries for the monitored service
resource "google_project_iam_member" "agent_log_viewer" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.infra_agent_sa.email}"
}

# Patch Cloud Run service config (memory, cpu, timeout, scaling)
resource "google_project_iam_member" "agent_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.infra_agent_sa.email}"
}

# Act as the order-api service account when deploying revisions
resource "google_service_account_iam_member" "agent_act_as_order_api" {
  service_account_id = google_service_account.order_api_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.infra_agent_sa.email}"
}

# Read images from Artifact Registry
resource "google_project_iam_member" "agent_artifactregistry_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.infra_agent_sa.email}"
}

# Read and write Firestore incidents collection
resource "google_project_iam_member" "agent_firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.infra_agent_sa.email}"
}

# ──────────────────────────────────────────────────────────────
# Service Account: bitbucket-deployer  (optional)
# Only created when var.bitbucket_service_account_email is empty,
# meaning we create a dedicated SA instead of reusing one.
# ──────────────────────────────────────────────────────────────
resource "google_service_account" "bitbucket_sa" {
  count        = var.bitbucket_service_account_email == "" ? 1 : 0
  account_id   = "bitbucket-deployer"
  display_name = "Bitbucket Pipelines Deployer"
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

locals {
  # If an external SA email is provided, use it; otherwise use the one we created.
  # count for the created SA is based purely on the variable (known at plan time).
  create_bitbucket_sa    = var.bitbucket_service_account_email == "" ? 1 : 0
  bitbucket_sa_email     = var.bitbucket_service_account_email != "" ? var.bitbucket_service_account_email : "bitbucket-deployer@${var.project_id}.iam.gserviceaccount.com"
}

# Push images to Artifact Registry
resource "google_project_iam_member" "bitbucket_registry_writer" {
  count   = local.create_bitbucket_sa
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${local.bitbucket_sa_email}"

  depends_on = [google_service_account.bitbucket_sa]
}

# Submit Cloud Build jobs
resource "google_project_iam_member" "bitbucket_cloud_build" {
  count   = local.create_bitbucket_sa
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${local.bitbucket_sa_email}"

  depends_on = [google_service_account.bitbucket_sa]
}

# Deploy Cloud Run revisions
resource "google_project_iam_member" "bitbucket_run_developer" {
  count   = local.create_bitbucket_sa
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${local.bitbucket_sa_email}"

  depends_on = [google_service_account.bitbucket_sa]
}

# Act as the Cloud Run service account during deploy
resource "google_service_account_iam_member" "bitbucket_act_as_order_api" {
  count              = local.create_bitbucket_sa
  service_account_id = google_service_account.order_api_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.bitbucket_sa_email}"

  depends_on = [google_service_account.bitbucket_sa]
}

# ──────────────────────────────────────────────────────────────
# Cloud Build service account — must be able to pull/push images
# ──────────────────────────────────────────────────────────────
resource "google_project_iam_member" "cloudbuild_registry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}

resource "google_service_account_iam_member" "cloudbuild_act_as_order_api" {
  service_account_id = google_service_account.order_api_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_project.project.number}@cloudbuild.gserviceaccount.com"
}
