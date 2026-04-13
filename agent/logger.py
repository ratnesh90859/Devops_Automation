from google.cloud import logging_v2
from datetime import datetime, timedelta, timezone
import subprocess, json
from config import settings

log_client = logging_v2.Client(project=settings.GCP_PROJECT_ID)


def fetch_logs(minutes: int = 5) -> str:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")

    filter_str = (
        f'resource.type="cloud_run_revision" '
        f'AND resource.labels.service_name="{settings.CLOUD_RUN_SERVICE}" '
        f'AND severity>=WARNING '
        f'AND timestamp>="{since}"'
    )

    entries = log_client.list_entries(
        filter_=filter_str,
        order_by=logging_v2.DESCENDING,
        max_results=80
    )

    lines = []
    for e in entries:
        ts = e.timestamp.strftime("%H:%M:%S")
        payload = e.payload if isinstance(e.payload, str) else str(e.payload)
        lines.append(f"[{ts}] {e.severity}: {payload}")

    return "\n".join(reversed(lines)) if lines else "No warning logs found."


def get_current_revision() -> str:
    result = subprocess.run([
        "gcloud", "run", "revisions", "list",
        "--service", settings.CLOUD_RUN_SERVICE,
        "--region", settings.GCP_REGION,
        "--project", settings.GCP_PROJECT_ID,
        "--format", "json", "--limit", "1"
    ], capture_output=True, text=True)
    revisions = json.loads(result.stdout or "[]")
    return revisions[0]["metadata"]["name"] if revisions else ""
