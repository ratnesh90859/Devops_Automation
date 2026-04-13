from google.cloud import firestore
from config import settings
from datetime import datetime, timezone
import uuid

# Uses GOOGLE_APPLICATION_CREDENTIALS env var (gcp-key.json mounted in Docker)
_db = firestore.Client(project=settings.GCP_PROJECT_ID)
_col = _db.collection("incidents")


def _doc_to_dict(doc) -> dict:
    d = doc.to_dict()
    d["id"] = doc.id
    return d


def create_incident(data: dict) -> dict:
    incident_id = str(uuid.uuid4())
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["status"] = "awaiting_approval"
    _col.document(incident_id).set(data)
    data["id"] = incident_id
    return data


def get_incident(incident_id: str) -> dict:
    doc = _col.document(incident_id).get()
    if not doc.exists:
        raise ValueError(f"Incident {incident_id} not found")
    return _doc_to_dict(doc)


def update_incident(incident_id: str, fields: dict) -> dict:
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    _col.document(incident_id).update(fields)
    return get_incident(incident_id)


def list_incidents() -> list:
    docs = (
        _col
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(20)
        .stream()
    )
    return [_doc_to_dict(d) for d in docs]


# No SQL setup needed — Firestore creates the collection automatically
# on first write. No schema, no migrations.
#
# Firestore path: projects/<GCP_PROJECT_ID>/databases/(default)/documents/incidents/<uuid>
#   created_at TIMESTAMPTZ DEFAULT NOW(),
#   updated_at TIMESTAMPTZ DEFAULT NOW()
# );
