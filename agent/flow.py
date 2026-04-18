import asyncio
import json
import re
from datetime import datetime, timezone
from logger import fetch_logs, fetch_all_loki_logs, get_current_revision
from cloudrun import get_config, is_healthy
import terraform_runner
import github_client as github
from ai import diagnose, analyze_deep, suggest_code_fix
from db import create_incident, get_incident, update_incident, get_latest_deployment, get_previous_deployment

# Path inside the GitHub repo that contains the app source
_APP_FILE = "infra-app/app.py"
# Path inside the GitHub repo for Terraform variables
_TFVARS_FILE = "terraform/terraform.tfvars"


async def handle_alert(source: str, service_url: str, alert_body: dict = None) -> dict:
    import httpx
    import json as _json

    previous_revision = get_current_revision()
    logs = fetch_logs(minutes=10)
    loki_logs = fetch_all_loki_logs(minutes=10)
    config = await get_config()

    # Also probe the live service to give Gemini more context
    live_status = "unknown"
    live_body = ""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(service_url)
            live_status = f"{r.status_code}"
            live_body = r.text[:500]
    except Exception as e:
        live_status = f"unreachable ({e})"

    # Build enriched context for AI
    context_parts = []

    # Include Grafana/source alert details so AI knows WHY the alert fired
    if alert_body:
        alert_summary = {
            k: v for k, v in alert_body.items()
            if k not in ("service_url",)
        }
        context_parts.append(
            f"ALERT PAYLOAD (from {source}):\n{_json.dumps(alert_summary, indent=2, default=str)[:1500]}"
        )

    # Include Cloud Run logs
    if "No warning logs" in logs:
        context_parts.append(
            f"CLOUD RUN LOGS: No recent warning/error logs found."
        )
    else:
        context_parts.append(f"CLOUD RUN LOGS:\n{logs}")

    # Include Loki structured logs (infra / app / business)
    if loki_logs.get("infra") and "unavailable" not in loki_logs["infra"]:
        context_parts.append(f"INFRASTRUCTURE LOGS (Loki):\n{loki_logs['infra'][:1000]}")
    if loki_logs.get("app") and "unavailable" not in loki_logs["app"]:
        context_parts.append(f"APPLICATION LOGS (Loki):\n{loki_logs['app'][:1000]}")
    if loki_logs.get("business") and "unavailable" not in loki_logs["business"]:
        context_parts.append(f"BUSINESS LOGS (Loki):\n{loki_logs['business'][:1000]}")

    # If the alert body carried inline log snapshots (threshold_monitor does this),
    # inject them so the AI can correlate all three layers even without Loki.
    if alert_body:
        for layer_key, label in (
            ("infra_logs",    "INFRASTRUCTURE SNAPSHOT (from alert)"),
            ("app_logs",      "APPLICATION SNAPSHOT (from alert)"),
            ("business_logs", "BUSINESS SNAPSHOT (from alert)"),
        ):
            val = alert_body.get(layer_key, "")
            if val and "unavailable" not in str(val):
                context_parts.append(f"{label}:\n{str(val)[:800]}")

        # Also surface numeric metrics from the payload for the AI
        numeric_fields = ["memory_mb", "total_requests", "total_errors", "error_rate_pct"]
        metrics_str = ", ".join(
            f"{f}={alert_body[f]}" for f in numeric_fields if f in alert_body
        )
        if metrics_str:
            context_parts.append(f"REAL-TIME METRICS (from alert): {metrics_str}")

    # Always include live probe results
    context_parts.append(
        f"LIVE SERVICE PROBE: {service_url} -> HTTP {live_status}\n"
        f"Response body: {live_body}"
    )

    enriched_logs = "\n\n".join(context_parts)

    # ── Deployment regression detection ──────────────────────────────────────
    # If a deployment happened in the last 30 minutes and an alert is now
    # firing, inject that context so the AI can classify it as a regression.
    rollback_image = ""
    try:
        latest_dep = get_latest_deployment()
        if latest_dep:
            dep_time = datetime.fromisoformat(latest_dep["deployed_at"])
            if dep_time.tzinfo is None:
                dep_time = dep_time.replace(tzinfo=timezone.utc)
            minutes_since = (datetime.now(timezone.utc) - dep_time).total_seconds() / 60
            if minutes_since < 30:
                rollback_image = latest_dep.get("image_tag", "")
                prev_dep = get_previous_deployment()
                rollback_target = prev_dep.get("image_tag", "") if prev_dep else rollback_image
                regression_context = (
                    f"\n\nDEPLOYMENT REGRESSION ALERT: A deployment occurred "
                    f"{minutes_since:.1f} min ago "
                    f"(commit: {latest_dep.get('commit_id', 'unknown')[:10]}, "
                    f"image: {latest_dep.get('image_tag', 'unknown')[-40:]}). "
                    f"This alert fired AFTER the deployment — HIGH PROBABILITY OF "
                    f"DEPLOYMENT REGRESSION. "
                    f"Rollback target: {rollback_target[-40:] if rollback_target else 'previous image'}\n"
                )
                enriched_logs = regression_context + enriched_logs
    except Exception as _reg_exc:
        print(f"[WARN] regression detection failed: {_reg_exc}")

    # Run fast diagnosis + deep SRE analysis in parallel
    diagnosis, deep_report = await asyncio.gather(
        diagnose(enriched_logs, config),
        analyze_deep(enriched_logs, config),
    )

    incident = create_incident({
        "source": source,
        "service_url": service_url,
        "previous_revision": previous_revision,
        "logs": enriched_logs[:2000],
        "live_status": live_status,
        "deep_report": deep_report,
        "loki_infra_logs": loki_logs.get("infra", ""),
        "loki_app_logs": loki_logs.get("app", ""),
        "loki_business_logs": loki_logs.get("business", ""),
        # Preserve the original alert body so the Telegram formatter can show
        # live metrics (total_requests, error_rate_pct, memory_mb, etc.)
        "alert_body": {
            k: v for k, v in (alert_body or {}).items()
            if k not in ("service_url",)
        },
        # Store rollback image so create_fix_pr can use it for regression fixes
        "rollback_image": rollback_image,
        **diagnosis
    })
    return incident


# ---------------------------------------------------------------------------
# Infra fix path (Terraform)
# ---------------------------------------------------------------------------

async def _infra_fix(incident: dict) -> dict:
    ok, tf_output = await terraform_runner.apply_fix(
        incident["fix_field"], incident["fix_new_value"]
    )
    if not ok:
        update_incident(incident["id"], {
            "status": "terraform_error",
            "terraform_output": tf_output,
        })
        return {"healthy": False, "error": tf_output}

    await asyncio.sleep(60)

    healthy = False
    for _ in range(3):
        healthy = await is_healthy()
        if healthy:
            break
        await asyncio.sleep(40)

    if healthy:
        update_incident(incident["id"], {"status": "resolved"})
        return {
            "healthy": True,
            "fix_field": incident["fix_field"],
            "fix_new_value": incident["fix_new_value"],
        }
    else:
        await terraform_runner.revert_fix(
            incident["fix_field"], incident["fix_old_value"]
        )
        update_incident(incident["id"], {"status": "rolled_back"})
        return {
            "healthy": False,
            "rolled_back_to": incident["fix_old_value"],
        }


# ---------------------------------------------------------------------------
# Code fix path (push to GitHub → pipeline → Cloud Run)
# ---------------------------------------------------------------------------

async def _code_fix(incident: dict) -> dict:
    """
    Flow:
      1. Fetch current app.py from GitHub
      2. Ask AI to generate a patched version
      3. Commit the patch to GitHub
      4. Wait for the GitHub pipeline to finish (build + push image)
      5. Wait for Cloud Run to pick up the new revision
      6. Health-check; rollback (revert file + re-pipeline) if unhealthy
    """
    update_incident(incident["id"], {"status": "fetching_code"})

    # 1. Get current file
    try:
        current_content = await github.get_file(_APP_FILE)
    except Exception as exc:
        update_incident(incident["id"], {
            "status": "GitHub_error",
            "GitHub_error": f"Could not fetch file: {exc}",
        })
        return {"healthy": False, "error": str(exc)}

    # 2. Ask AI for a code fix
    fix_suggestion = await suggest_code_fix(
        incident["issue_type"],
        incident.get("logs", ""),
        current_content,
    )

    if not fix_suggestion.get("needs_code_fix"):
        # AI says this is actually an infra problem after all — fall back
        update_incident(incident["id"], {"status": "downgraded_to_infra"})
        return await _infra_fix(incident)

    fixed_content = fix_suggestion["fixed_content"]
    commit_msg = fix_suggestion.get("commit_message", "fix: ai-generated patch")
    update_incident(incident["id"], {
        "status": "pushing_code",
        "code_explanation": fix_suggestion.get("explanation", ""),
        "commit_message": commit_msg,
    })

    # 3. Commit the patch
    committed = await github.commit_file(_APP_FILE, fixed_content, commit_msg)
    if not committed:
        update_incident(incident["id"], {"status": "GitHub_commit_failed"})
        return {"healthy": False, "error": "GitHub commit failed"}

    # 4. The commit triggers the pipeline automatically; wait for it
    update_incident(incident["id"], {"status": "waiting_pipeline"})
    pipeline_uuid = await github.trigger_pipeline()
    pipeline_ok = await github.wait_for_pipeline(pipeline_uuid, timeout=600)

    if not pipeline_ok:
        update_incident(incident["id"], {
            "status": "pipeline_failed",
            "pipeline_uuid": pipeline_uuid,
        })
        # Attempt rollback: restore previous content and re-trigger
        await github.commit_file(
            _APP_FILE, current_content, "revert: pipeline failed, restoring previous code"
        )
        rollback_uuid = await github.trigger_pipeline()
        await github.wait_for_pipeline(rollback_uuid, timeout=600)
        update_incident(incident["id"], {"status": "rolled_back"})
        return {"healthy": False, "pipeline_failed": True}

    # 5. Give Cloud Run time to pull and deploy the new image
    update_incident(incident["id"], {"status": "waiting_deploy"})
    await asyncio.sleep(60)

    # 6. Health check
    healthy = False
    for _ in range(3):
        healthy = await is_healthy()
        if healthy:
            break
        await asyncio.sleep(40)

    if healthy:
        update_incident(incident["id"], {
            "status": "resolved",
            "pipeline_uuid": pipeline_uuid,
        })
        return {
            "healthy": True,
            "fix_type": "code",
            "commit_message": commit_msg,
            "pipeline_uuid": pipeline_uuid,
        }
    else:
        # Rollback: restore previous file content and redeploy
        await github.commit_file(
            _APP_FILE, current_content, "revert: unhealthy after code fix, restoring previous code"
        )
        rollback_uuid = await github.trigger_pipeline()
        await github.wait_for_pipeline(rollback_uuid, timeout=600)
        update_incident(incident["id"], {"status": "rolled_back"})
        return {"healthy": False, "rolled_back": True}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def execute_fix(incident_id: str) -> dict:
    incident = get_incident(incident_id)
    update_incident(incident_id, {"status": "applying"})

    fix_type = incident.get("fix_type", "infra")
    if fix_type == "code":
        return await _code_fix(incident)
    else:
        return await _infra_fix(incident)


# ---------------------------------------------------------------------------
# PR-based approval flow  (enterprise path — no direct Terraform apply)
# ---------------------------------------------------------------------------

def _build_pr_description(incident: dict) -> str:
    """Build a rich PR description with root cause summary and fix details."""
    fix_type    = incident.get("fix_type", "infra")
    issue_type  = incident.get("issue_type", "unknown")
    confidence  = int(float(incident.get("confidence", 0) or 0) * 100)
    fix_display = (
        f"`{incident.get('fix_field', 'N/A')}`: "
        f"`{incident.get('fix_old_value', 'N/A')}` → `{incident.get('fix_new_value', 'N/A')}`"
        if fix_type in ("infra", "rollback")
        else "Patched application code (see diff)"
    )
    pipeline_note = (
        "**terraform init + plan + apply** will run (targets Cloud Run service only)"
        if fix_type == "infra"
        else "**gcloud run deploy** with previous image tag will run"
        if fix_type == "rollback"
        else "**docker build + gcloud run deploy** will run"
    )
    return f"""## 🤖 AI-Detected Incident — Automated Fix PR

| Field | Value |
|---|---|
| Incident ID | `{incident.get('id', 'N/A')}` |
| Issue Type | `{issue_type}` |
| Severity | {incident.get('severity', 'N/A')} |
| Confidence | {confidence}% |
| Fix Type | {fix_type} |

### Root Cause
{incident.get('root_cause', 'N/A')}

### Proposed Change
{fix_display}

**Reason:** {incident.get('fix_reason', 'N/A')}

### What happens when you merge this PR
{pipeline_note}

### Evidence (log excerpt)
```
{incident.get('logs', '')[:600]}
```

---
*This PR was automatically created by the AI Ops Agent.*  
*Review the changes carefully before approving.*  
*Pipeline triggers automatically on merge — no manual steps needed.*
"""


async def create_fix_pr(incident: dict) -> dict:
    """
    Enterprise approval path:
      1. Create a fix/* branch from main
      2. Commit the change files + .fix-meta.json to the branch
      3. Open a Pull Request describing the incident and proposed fix
      4. Return {"pr_url", "pr_id", "branch", "fix_type"}

    The pipeline triggers automatically AFTER the PR is merged.
    No Terraform apply or deploy happens here — only branch + PR creation.

    Fix types:
      infra      → commits updated terraform/terraform.tfvars
      rollback   → commits .fix-meta.json with rollback_image
      code       → commits AI-patched infra-app/app.py
    """
    incident_id = incident["id"]
    fix_type    = incident.get("fix_type", "infra")
    issue_type  = incident.get("issue_type", "unknown")
    ts          = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch_name = f"fix/{issue_type}-{ts}"

    update_incident(incident_id, {"status": "creating_pr_branch"})

    # ── 1. Create branch ─────────────────────────────────────────────────────
    created = await github.create_branch(branch_name)
    if not created:
        raise RuntimeError(f"Failed to create branch '{branch_name}' — check GitHub credentials")

    update_incident(incident_id, {"fix_branch": branch_name})

    files: dict = {}

    # ── 2. Build the files to commit based on fix type ───────────────────────
    if fix_type == "infra":
        # Fetch current tfvars from GitHub and patch the fix field
        try:
            tfvars_content = await github.get_file(_TFVARS_FILE)
        except Exception as exc:
            raise RuntimeError(f"Could not fetch {_TFVARS_FILE}: {exc}")

        fix_field     = incident.get("fix_field", "memory")
        fix_new_value = incident.get("fix_new_value", "512Mi")
        tfvar_key     = terraform_runner.FIELD_TO_TFVAR.get(fix_field, f"cloudrun_{fix_field}")
        is_int        = tfvar_key in terraform_runner._INTEGER_VARS
        formatted     = fix_new_value if is_int else f'"{fix_new_value}"'
        pattern       = rf'^({re.escape(tfvar_key)}\s*=\s*).*$'
        new_tfvars    = re.sub(pattern, f'{tfvar_key} = {formatted}', tfvars_content,
                               flags=re.MULTILINE)

        files[_TFVARS_FILE] = new_tfvars
        files[".fix-meta.json"] = json.dumps({
            "fix_type":      "infra",
            "fix_field":     fix_field,
            "fix_old_value": incident.get("fix_old_value", ""),
            "fix_new_value": fix_new_value,
            "incident_id":   incident_id,
            "issue_type":    issue_type,
        }, indent=2)
        commit_msg = (
            f"fix({issue_type}): {fix_field} "
            f"{incident.get('fix_old_value','')} → {fix_new_value}\n\n"
            f"AI-detected {issue_type} incident {incident_id[:8]}. "
            f"{incident.get('root_cause', '')[:120]}"
        )

    elif fix_type == "rollback":
        rollback_image = incident.get("rollback_image", "")
        files[".fix-meta.json"] = json.dumps({
            "fix_type":       "rollback",
            "rollback_image": rollback_image,
            "incident_id":    incident_id,
            "issue_type":     issue_type,
        }, indent=2)
        commit_msg = (
            f"fix(rollback): revert deployment regression\n\n"
            f"AI detected regression in incident {incident_id[:8]}. "
            f"Rolling back to: {rollback_image[-50:] if rollback_image else 'previous image'}"
        )

    else:  # code fix
        try:
            current_content = await github.get_file(_APP_FILE)
        except Exception as exc:
            raise RuntimeError(f"Could not fetch {_APP_FILE}: {exc}")

        fix_suggestion = await suggest_code_fix(
            incident["issue_type"],
            incident.get("logs", ""),
            current_content,
        )

        if not fix_suggestion.get("needs_code_fix"):
            # AI says it's actually an infra issue — re-classify and retry
            incident = dict(incident)
            incident["fix_type"] = "infra"
            update_incident(incident_id, {"fix_type": "infra", "status": "downgraded_to_infra"})
            return await create_fix_pr(incident)

        fixed_content = fix_suggestion["fixed_content"]
        commit_msg    = fix_suggestion.get("commit_message", f"fix(code): ai-patch for {issue_type}")
        update_incident(incident_id, {
            "code_explanation": fix_suggestion.get("explanation", ""),
        })
        files[_APP_FILE] = fixed_content
        files[".fix-meta.json"] = json.dumps({
            "fix_type":    "code",
            "incident_id": incident_id,
            "issue_type":  issue_type,
        }, indent=2)

    # ── 3. Commit files to the fix branch ────────────────────────────────────
    committed = await github.commit_to_branch(branch_name, files, commit_msg)
    if not committed:
        raise RuntimeError(f"Failed to commit files to branch '{branch_name}'")

    # ── 4. Create the Pull Request ───────────────────────────────────────────
    pr_title       = f"fix({issue_type}): {incident.get('fix_field', fix_type)} change"
    pr_description = _build_pr_description(incident)
    pr             = await github.create_pr(pr_title, pr_description, branch_name)
    if not pr:
        raise RuntimeError("GitHub PR creation failed — check API token permissions")

    update_incident(incident_id, {
        "status":     "pr_created",
        "pr_id":      pr["id"],
        "pr_url":     pr["url"],
        "fix_branch": branch_name,
    })

    return {
        "pr_url":   pr["url"],
        "pr_id":    pr["id"],
        "branch":   branch_name,
        "fix_type": fix_type,
    }


async def reject(incident_id: str, reason: str = ""):
    fields: dict = {"status": "rejected"}
    if reason:
        fields["rejection_reason"] = reason
    update_incident(incident_id, fields)
    # Clear the dedup lock so new alerts can come through
    _clear_active_incident()


def _clear_active_incident():
    """Clear the dedup lock in main.py so new alerts are accepted."""
    try:
        import main as _main
        import asyncio
        async def _clear():
            async with _main._active_incident_lock:
                _main._active_incident = None
        # If we're already in an event loop, schedule it
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_clear())
        except RuntimeError:
            asyncio.run(_clear())
    except Exception:
        pass  # best-effort
