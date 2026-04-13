"""
terraform_runner.py
-------------------
All Cloud Run config changes go through Terraform.
The agent NEVER calls the Cloud Run REST API to patch settings directly.

Flow:
  1. AI diagnoses issue  → suggests fix_field + fix_new_value
  2. Human approves via Telegram
  3. apply_fix()  → updates terraform.tfvars  → terraform apply
  4. Health check (cloudrun.is_healthy)
  5. If unhealthy → revert_fix() → revert tfvars → terraform apply
"""
import asyncio
import os
import re
import subprocess

from config import settings

# Maps AI fix_field → Terraform variable name in terraform.tfvars
FIELD_TO_TFVAR: dict[str, str] = {
    "memory":        "cloudrun_memory",
    "timeout":       "cloudrun_timeout",
    "cpu":           "cloudrun_cpu",
    "min_instances": "cloudrun_min_instances",
    "max_instances": "cloudrun_max_instances",
}

# These tfvars are integers (no quotes in HCL)
_INTEGER_VARS = {
    "cloudrun_min_instances",
    "cloudrun_max_instances",
    "cloudrun_timeout",
    "cloudrun_concurrency",
}


def _tfvars_path() -> str:
    return os.path.join(settings.TERRAFORM_DIR, "terraform.tfvars")


def read_tfvars() -> dict:
    """Return current terraform.tfvars as a plain dict."""
    result: dict = {}
    with open(_tfvars_path()) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                result[key.strip()] = val.strip().strip('"')
    return result


def write_tfvar(key: str, value: str) -> None:
    """Update an existing variable or append it to terraform.tfvars."""
    with open(_tfvars_path()) as f:
        content = f.read()

    formatted = value if key in _INTEGER_VARS else f'"{value}"'
    pattern = rf'^({re.escape(key)}\s*=\s*).*$'

    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f'{key} = {formatted}', content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{key} = {formatted}\n"

    with open(_tfvars_path(), "w") as f:
        f.write(content)


def _ensure_init() -> None:
    """Run `terraform init` once if the .terraform directory is missing."""
    dot_tf = os.path.join(settings.TERRAFORM_DIR, ".terraform")
    if not os.path.isdir(dot_tf):
        subprocess.run(
            ["terraform", "init"],
            cwd=settings.TERRAFORM_DIR,
            capture_output=True,
            text=True,
            check=True,
        )


async def _run_apply() -> tuple[bool, str]:
    """
    Run terraform apply targeting only the Cloud Run service.
    Returns (success, output_snippet).
    """
    _ensure_init()
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "terraform", "apply",
            "-auto-approve",
            "-target=google_cloud_run_v2_service.order_api",
        ],
        capture_output=True,
        text=True,
        cwd=settings.TERRAFORM_DIR,
    )
    output = (result.stderr or result.stdout or "")[:1000]
    return result.returncode == 0, output


async def apply_fix(field: str, new_value: str) -> tuple[bool, str]:
    """
    Write new_value for the given fix_field to tfvars, then terraform apply.
    Called after human approves the fix in Telegram.
    """
    tfvar = FIELD_TO_TFVAR.get(field)
    if not tfvar:
        return False, f"Unknown fix field: {field}"
    write_tfvar(tfvar, new_value)
    return await _run_apply()


async def revert_fix(field: str, old_value: str) -> tuple[bool, str]:
    """
    Revert tfvars to old_value and terraform apply.
    Called automatically when health checks fail after a fix.
    """
    tfvar = FIELD_TO_TFVAR.get(field)
    if not tfvar:
        return False, f"Unknown fix field: {field}"
    write_tfvar(tfvar, old_value)
    return await _run_apply()
