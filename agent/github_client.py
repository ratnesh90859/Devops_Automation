"""
github_client.py
----------------
GitHub REST API v3 client — drop-in replacement for bitbucket.py.

Responsibilities:
  - Fetch file content from the repo
  - Commit a patched file to a branch
  - Create a branch
  - Open a Pull Request
  - Trigger a workflow dispatch
  - Poll workflow run status

Credentials needed (add to Cloud Run env vars):
  GITHUB_OWNER       - GitHub username or org  (e.g. "ratnesh90859")
  GITHUB_REPO        - Repository name         (e.g. "Devops_Automation")
  GITHUB_TOKEN       - Personal Access Token   (repo + workflow scopes)
  GITHUB_BRANCH      - default branch          (default: "main")
"""

import asyncio
import os
import httpx
from config import settings

_BASE = "https://api.github.com"


def _token() -> str:
    # Read directly from os.environ so a pydantic-settings default of ""
    # never silently shadows the Cloud Run env var.
    return os.environ.get("GITHUB_TOKEN", "") or settings.GITHUB_TOKEN


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    owner = os.environ.get("GITHUB_OWNER", "") or settings.GITHUB_OWNER
    repo  = os.environ.get("GITHUB_REPO",  "") or settings.GITHUB_REPO
    return f"{owner}/{repo}"


def _branch() -> str:
    return os.environ.get("GITHUB_BRANCH", "") or settings.GITHUB_BRANCH or "main"


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

async def get_file(file_path: str) -> str:
    """Return the raw text content of *file_path* from the configured branch."""
    url = f"{_BASE}/repos/{_repo()}/contents/{file_path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            url,
            headers=_headers(),
            params={"ref": _branch()},
        )
        r.raise_for_status()
        import base64
        content_b64 = r.json()["content"].replace("\n", "")
        return base64.b64decode(content_b64).decode()


async def _get_file_sha(file_path: str, branch: str) -> str:
    """Return the blob SHA of a file on a branch (needed for PUT updates)."""
    url = f"{_BASE}/repos/{_repo()}/contents/{file_path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers(), params={"ref": branch})
        if r.status_code == 404:
            return ""          # file doesn't exist yet
        r.raise_for_status()
        return r.json().get("sha", "")


async def commit_file(file_path: str, new_content: str, message: str,
                      branch: str | None = None) -> bool:
    """
    Create or update *file_path* on *branch* with *new_content*.
    Returns True on success.
    """
    import base64
    target_branch = branch or _branch()
    sha = await _get_file_sha(file_path, target_branch)
    url = f"{_BASE}/repos/{_repo()}/contents/{file_path}"
    payload: dict = {
        "message": message,
        "content": base64.b64encode(new_content.encode()).decode(),
        "branch": target_branch,
    }
    if sha:
        payload["sha"] = sha
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, headers=_headers(), json=payload)
        return r.status_code in (200, 201)


async def commit_to_branch(branch_name: str, files: dict, commit_message: str) -> bool:
    """
    Commit multiple files to *branch_name* in a single logical operation.
    GitHub doesn't have a batch endpoint so we commit them sequentially.
    Returns True only if ALL commits succeed.
    """
    for file_path, content in files.items():
        ok = await commit_file(file_path, content, commit_message, branch=branch_name)
        if not ok:
            print(f"[ERROR] commit_to_branch: failed on {file_path}")
            return False
    return True


# ---------------------------------------------------------------------------
# Branch operations
# ---------------------------------------------------------------------------

async def _get_branch_sha(branch: str) -> str:
    """Return the HEAD commit SHA of *branch*."""
    url = f"{_BASE}/repos/{_repo()}/git/ref/heads/{branch}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers())
        r.raise_for_status()
        return r.json()["object"]["sha"]


async def create_branch(branch_name: str, from_branch: str | None = None) -> bool:
    """
    Create *branch_name* from *from_branch* (defaults to GITHUB_BRANCH).
    Returns True on success.
    """
    from_ref = from_branch or _branch()
    try:
        sha = await _get_branch_sha(from_ref)
    except Exception as exc:
        print(f"[ERROR] create_branch: could not get SHA of {from_ref!r}: {exc}")
        return False

    url = f"{_BASE}/repos/{_repo()}/git/refs"
    payload = {"ref": f"refs/heads/{branch_name}", "sha": sha}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code not in (200, 201):
            print(f"[ERROR] create_branch {branch_name!r}: {r.status_code} {r.text[:200]}")
            return False
        return True


# ---------------------------------------------------------------------------
# Pull Request operations
# ---------------------------------------------------------------------------

async def create_pr(title: str, description: str, head_branch: str,
                    base_branch: str | None = None) -> dict | None:
    """
    Open a Pull Request from *head_branch* → *base_branch*.
    Returns {"id": ..., "url": ..., "number": ...} or None on failure.
    """
    base = base_branch or _branch()
    url = f"{_BASE}/repos/{_repo()}/pulls"
    payload = {
        "title": title,
        "body": description,
        "head": head_branch,
        "base": base,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code not in (200, 201):
            print(f"[ERROR] create_pr: {r.status_code} {r.text[:300]}")
            return None
        data = r.json()
        return {
            "id":     data["number"],
            "number": data["number"],
            "url":    data["html_url"],
        }


# ---------------------------------------------------------------------------
# Workflow / pipeline operations
# ---------------------------------------------------------------------------

async def trigger_pipeline(branch: str | None = None) -> str:
    """
    Trigger the 'deploy.yml' workflow via workflow_dispatch on *branch*.
    Returns a workflow run ID string (polled after dispatch).
    GitHub dispatch doesn't return a run ID directly, so we poll for it.
    """
    target_branch = branch or _branch()
    url = f"{_BASE}/repos/{_repo()}/actions/workflows/deploy.yml/dispatches"
    payload = {"ref": target_branch}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code != 204:
            raise RuntimeError(
                f"workflow_dispatch failed: {r.status_code} {r.text[:200]}"
            )

    # Poll runs list to find the run that just started
    await asyncio.sleep(5)
    runs_url = f"{_BASE}/repos/{_repo()}/actions/workflows/deploy.yml/runs"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            runs_url,
            headers=_headers(),
            params={"branch": target_branch, "per_page": 1},
        )
        r.raise_for_status()
        runs = r.json().get("workflow_runs", [])
        if runs:
            return str(runs[0]["id"])
    return "unknown"


async def get_pipeline_status(run_id: str) -> str:
    """
    Return normalised status for a workflow run.
    Maps GitHub statuses → Bitbucket-compatible names so flow.py works unchanged:
      SUCCESSFUL / FAILED / IN_PROGRESS / UNKNOWN
    """
    if run_id == "unknown":
        return "UNKNOWN"
    url = f"{_BASE}/repos/{_repo()}/actions/runs/{run_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=_headers())
        r.raise_for_status()
        data = r.json()
    status     = data.get("status", "")      # queued / in_progress / completed
    conclusion = data.get("conclusion", "")  # success / failure / cancelled / None

    if status == "completed":
        return "SUCCESSFUL" if conclusion == "success" else "FAILED"
    return "IN_PROGRESS"


async def wait_for_pipeline(run_id: str, timeout: int = 600) -> bool:
    """
    Poll every 20 s until the run finishes or *timeout* seconds elapse.
    Returns True → SUCCESSFUL, False → FAILED / timeout.
    """
    elapsed = 0
    interval = 20
    while elapsed < timeout:
        status = await get_pipeline_status(run_id)
        if status == "SUCCESSFUL":
            return True
        if status == "FAILED":
            return False
        await asyncio.sleep(interval)
        elapsed += interval
    return False
