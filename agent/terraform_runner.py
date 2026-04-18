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

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip_ansi(text: str) -> str:
    """Remove ANSI color/escape codes from terraform output."""
    return _ANSI_RE.sub('', text)

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


def _try_force_unlock(error_output: str) -> bool:
    """If terraform failed due to a stale state lock, force-unlock and return True."""
    m = re.search(r'ID:\s+(\d+)', error_output)
    if not m:
        return False
    lock_id = m.group(1)
    print(f"[terraform] Auto-unlocking stale lock {lock_id}")
    r = subprocess.run(
        ["terraform", "force-unlock", "-force", lock_id],
        capture_output=True, text=True,
        cwd=settings.TERRAFORM_DIR,
    )
    return r.returncode == 0


def _summarize_error(raw: str) -> str:
    """Extract a clean human-readable error from terraform output."""
    clean = _strip_ansi(raw).strip()
    # Extract the core Error line
    error_lines = []
    for line in clean.splitlines():
        line = line.strip().lstrip('│').strip()
        if not line:
            continue
        if line.startswith('╷') or line.startswith('╵'):
            continue
        error_lines.append(line)
    summary = '\n'.join(error_lines[:10])
    return summary[:500] if summary else clean[:500]


async def _run_apply() -> tuple[bool, str]:
    """
    Run terraform apply targeting only the Cloud Run service.
    Auto-retries once if a stale state lock is detected.
    Returns (success, clean_output).
    """
    _ensure_init()

    for attempt in range(2):
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
        raw = (result.stderr or result.stdout or "")
        if result.returncode == 0:
            return True, _strip_ansi(raw)[:500]

        # If state lock error, auto-unlock and retry once
        if "Error acquiring the state lock" in raw and attempt == 0:
            if _try_force_unlock(raw):
                print("[terraform] Lock cleared, retrying apply...")
                continue

        return False, _summarize_error(raw)

    return False, "Terraform apply failed after retry"


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


# Baseline values — what the service should look like before any AI fix
_BASELINE: dict[str, str] = {
    "cloudrun_memory":        "256Mi",
    "cloudrun_cpu":           "1",
    "cloudrun_timeout":       "30",
    "cloudrun_min_instances": "0",
    "cloudrun_max_instances": "3",
}


async def reset_to_baseline() -> tuple[bool, str]:
    """
    Reset ALL terraform.tfvars values back to baseline so you can retest
    from a clean state. Runs terraform apply after writing baseline values.
    Returns (success, output_summary).
    """
    current = read_tfvars()
    changed: list[str] = []

    for tfvar, baseline_value in _BASELINE.items():
        current_value = current.get(tfvar, "")
        if current_value != baseline_value:
            write_tfvar(tfvar, baseline_value)
            changed.append(f"{tfvar}: {current_value!r} → {baseline_value!r}")
        else:
            # Always write baseline values so tfvars matches reality even when
            # a previous container revision applied a fix (tfvars reset on restart)
            write_tfvar(tfvar, baseline_value)

    # Always run terraform apply — the actual Cloud Run service may differ from
    # the tfvars file if a previous container revision applied a fix and was then
    # replaced (new revision starts with the image-baked tfvars, not the modified one).
    ok, output = await _run_apply()
    if not changed:
        summary = "Terraform re-applied to ensure Cloud Run matches baseline."
    else:
        summary = "Reset changes:\n" + "\n".join(f"  • {c}" for c in changed)
    if ok:
        return True, f"Baseline restored.\n{summary}"
    return False, f"Terraform apply failed after baseline reset.\n{summary}\n\nError:\n{output}"
