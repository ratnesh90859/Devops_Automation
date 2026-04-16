from google.cloud import logging_v2
from datetime import datetime, timedelta, timezone
import subprocess, json, time
import httpx
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


# ── Loki log fetchers ─────────────────────────────────────────────────────────

def _loki_query(query: str, minutes: int = 10, limit: int = 100) -> str:
    """Query Loki and return log lines as a single string."""
    loki_url = getattr(settings, "LOKI_URL", "")
    if not loki_url:
        return "Loki not configured."

    since_ns = int((time.time() - minutes * 60) * 1e9)
    now_ns   = int(time.time() * 1e9)

    try:
        resp = httpx.get(
            f"{loki_url}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(since_ns),
                "end":   str(now_ns),
                "limit": limit,
                "direction": "forward",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        lines = []
        for stream in data.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9).strftime("%H:%M:%S")
                lines.append(f"[{ts}] {line}")
        return "\n".join(lines) if lines else "No logs found in Loki."
    except Exception as exc:
        return f"Loki query failed: {exc}"


def fetch_infra_logs(service: str = "order-api", minutes: int = 10) -> str:
    """
    Fetch infrastructure logs from Loki.
    These are lines tagged [INFRA] — memory spikes, CPU events, container restarts.
    """
    query = f'{{service="{service}", log_type="INFRA"}}'
    result = _loki_query(query, minutes=minutes, limit=100)
    if "not configured" in result or "failed" in result:
        # Fallback to GCP Cloud Logging
        return fetch_logs(minutes=minutes)
    return result if result != "No logs found in Loki." else "No recent infrastructure events."


def fetch_app_logs(service: str = "order-api", minutes: int = 10) -> str:
    """
    Fetch application logs from Loki.
    These are lines tagged [APP] — exceptions, slow requests, HTTP 5xx errors.
    """
    query = f'{{service="{service}", log_type="APP"}}'
    result = _loki_query(query, minutes=minutes, limit=100)
    if "not configured" in result or "failed" in result:
        return fetch_logs(minutes=minutes)
    return result if result != "No logs found in Loki." else "No recent application errors."


def fetch_business_logs(service: str = "order-api", minutes: int = 10) -> str:
    """
    Fetch business logs from Loki.
    These are lines tagged [BIZ] — order placed, order failed, payment events.
    """
    query = f'{{service="{service}", log_type="BIZ"}}'
    result = _loki_query(query, minutes=minutes, limit=100)
    if "not configured" in result or "failed" in result:
        return "Business logs unavailable (Loki not reachable)."
    return result if result != "No logs found in Loki." else "No recent business events."


def fetch_all_loki_logs(service: str = "order-api", minutes: int = 10) -> dict:
    """
    Fetch all three log streams in one call.
    Returns dict with keys: infra, app, business
    """
    return {
        "infra":    fetch_infra_logs(service, minutes),
        "app":      fetch_app_logs(service, minutes),
        "business": fetch_business_logs(service, minutes),
    }


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
