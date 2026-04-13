"""
bitbucket.py
------------
Bitbucket Cloud REST API v2 client.

Responsibilities:
  - Fetch file content from the repo
  - Commit a patched file back to the repo
  - Trigger a Bitbucket pipeline on a branch
  - Poll pipeline status until it completes

Credentials needed (add to .env):
  BITBUCKET_WORKSPACE  - e.g. "code_econz"
  BITBUCKET_REPO_SLUG  - e.g. "devops_automation"
  BITBUCKET_API_TOKEN  - Atlassian API Token (account.atlassian.com → Security → API tokens)
  BITBUCKET_BRANCH     - branch to commit/trigger (default: "main")
"""
import asyncio
import httpx
from config import settings

_BASE = "https://api.bitbucket.org/2.0"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.BITBUCKET_API_TOKEN}"}


def _repo() -> str:
    return f"{settings.BITBUCKET_WORKSPACE}/{settings.BITBUCKET_REPO_SLUG}"


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

async def get_file(file_path: str) -> str:
    """
    Return the raw text content of *file_path* from the configured branch.

    Example:
        content = await get_file("infra-app/app.py")
    """
    url = (
        f"{_BASE}/repositories/{_repo()}/src/"
        f"{settings.BITBUCKET_BRANCH}/{file_path}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers())
        r.raise_for_status()
        return r.text


async def commit_file(file_path: str, new_content: str, message: str) -> bool:
    """
    Commit *new_content* as *file_path* on the configured branch.

    Bitbucket's source-write endpoint accepts multipart/form-data where each
    additional key that isn't a control field is treated as a file path whose
    value is the new content.

    Returns True on success, False otherwise.
    """
    url = f"{_BASE}/repositories/{_repo()}/src"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            headers=_headers(),
            data={
                "message": message,
                "branch": settings.BITBUCKET_BRANCH,
                file_path: new_content,
            },
        )
        return r.status_code in (200, 201)


# ---------------------------------------------------------------------------
# Pipeline operations
# ---------------------------------------------------------------------------

async def trigger_pipeline(branch: str | None = None) -> str:
    """
    Trigger a pipeline on *branch* (defaults to BITBUCKET_BRANCH).
    Returns the pipeline UUID string, e.g. "{abc-123}".
    """
    branch = branch or settings.BITBUCKET_BRANCH
    url = f"{_BASE}/repositories/{_repo()}/pipelines/"
    payload = {
        "target": {
            "ref_type": "branch",
            "type": "pipeline_ref_target",
            "ref_name": branch,
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        r.raise_for_status()
        return r.json()["uuid"]


async def get_pipeline_status(pipeline_uuid: str) -> str:
    """
    Return the top-level state name of a pipeline.

    Possible values: IN_PROGRESS, PENDING, PAUSED, SUCCESSFUL, FAILED,
                     ERROR, STOPPED
    """
    url = f"{_BASE}/repositories/{_repo()}/pipelines/{pipeline_uuid}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers())
        r.raise_for_status()
        return r.json().get("state", {}).get("name", "UNKNOWN")


async def wait_for_pipeline(pipeline_uuid: str, timeout: int = 600) -> bool:
    """
    Poll every 20 s until the pipeline finishes or *timeout* seconds elapse.

    Returns True  → SUCCESSFUL
            False → FAILED / ERROR / STOPPED / timeout
    """
    terminal_states = {"SUCCESSFUL", "FAILED", "ERROR", "STOPPED"}
    elapsed = 0
    interval = 20

    while elapsed < timeout:
        status = await get_pipeline_status(pipeline_uuid)
        if status == "SUCCESSFUL":
            return True
        if status in terminal_states:
            return False
        await asyncio.sleep(interval)
        elapsed += interval

    return False  # timed out
