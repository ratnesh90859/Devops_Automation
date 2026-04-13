# Cloud Firestore database (Native mode)
# Replaces Supabase — stores all incident records.
# The "incidents" collection is created automatically on first write.

resource "google_firestore_database" "default" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Prevent accidental deletion of incident history
  deletion_policy = "DELETE"

  depends_on = [google_project_service.apis]
}

# Single-field index on created_at is created automatically by Firestore.
# No composite index needed for ORDER BY created_at DESC alone.
