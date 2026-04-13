import httpx, subprocess, asyncio
from google.auth import default
from google.auth.transport.requests import Request
from config import settings

BASE = (
    f"https://run.googleapis.com/v2/projects/{settings.GCP_PROJECT_ID}"
    f"/locations/{settings.GCP_REGION}/services/{settings.CLOUD_RUN_SERVICE}"
)


def _token() -> str:
    creds, _ = default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return creds.token


async def get_config() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(BASE, headers={"Authorization": f"Bearer {_token()}"})
        r.raise_for_status()
        d = r.json()
    t = d.get("template", {})
    container = t.get("containers", [{}])[0]
    limits = container.get("resources", {}).get("limits", {})
    scaling = t.get("scaling", {})
    return {
        "memory": limits.get("memory", "256Mi"),
        "cpu": limits.get("cpu", "1"),
        "timeout": t.get("timeout", "30s").replace("s", ""),
        "min_instances": scaling.get("minInstanceCount", 0),
        "max_instances": scaling.get("maxInstanceCount", 5),
    }



async def is_healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.CLOUD_RUN_SERVICE_URL}/")
            return r.status_code == 200
    except Exception:
        return False
