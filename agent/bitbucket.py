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
import base64
import httpx
from config import settings

_BASE = "https://api.bitbucket.org/2.0"


def _headers() -> dict:
    credentials = base64.b64encode(
        f"{settings.BITBUCKET_USERNAME}:{settings.BITBUCKET_API_TOKEN}".encode()
    ).decode()
    return {"Authorization": f"Basic {credentials}"}


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


# ---------------------------------------------------------------------------
# Branch + PR operations (PR-based approval flow)
# ---------------------------------------------------------------------------

async def create_branch(branch_name: str, from_branch: str | None = None) -> bool:
    """
    Create a new git branch from from_branch (defaults to BITBUCKET_BRANCH).
    Used to create fix/* branches before committing changes and opening a PR.
    """
    from_ref = from_branch or settings.BITBUCKET_BRANCH
    url = f"{_BASE}/repositories/{_repo()}/refs/branches"
    payload = {"name": branch_name, "target": {"hash": from_ref}}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code not in (200, 201):
            print(f"[ERROR] create_branch {branch_name!r}: {r.status_code} {r.text[:200]}")
            return False
        return True


async def commit_to_branch(branch: str, files: dict, message: str) -> bool:
    """
    Commit one or more files to a specific branch in a single Bitbucket API call.

    files: {"path/to/file.tf": "content", ".fix-meta.json": "content", ...}

    Uses the same /src multipart endpoint as commit_file but targets an
    explicit branch instead of the default BITBUCKET_BRANCH.
    """
    url = f"{_BASE}/repositories/{_repo()}/src"
    data = {"message": message, "branch": branch}
    data.update(files)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), data=data)
        if r.status_code not in (200, 201):
            print(f"[ERROR] commit_to_branch {branch!r}: {r.status_code} {r.text[:200]}")
            return False
        return True


async def create_pr(title: str, description: str, source_branch: str,
                    dest_branch: str | None = None) -> dict:
    """
    Create a pull request from source_branch → dest_branch.

    Returns {"id": <int>, "url": <str>} on success, {} on failure.
    The PR is configured to auto-close the source branch on merge.
    """
    dest = dest_branch or settings.BITBUCKET_BRANCH
    url = f"{_BASE}/repositories/{_repo()}/pullrequests"
    payload = {
        "title": title,
        "description": description,
        "source": {"branch": {"name": source_branch}},
        "destination": {"branch": {"name": dest}},
        "close_source_branch": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code in (200, 201):
            data = r.json()
            return {
                "id":  data["id"],
                "url": data["links"]["html"]["href"],
            }
        print(f"[ERROR] create_pr failed: {r.status_code} {r.text[:300]}")
        return {}
