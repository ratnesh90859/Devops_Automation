import asyncio
from logger import fetch_logs, fetch_all_loki_logs, get_current_revision
from cloudrun import get_config, is_healthy
import terraform_runner
import bitbucket
from ai import diagnose, analyze_deep, suggest_code_fix
from db import create_incident, get_incident, update_incident

# Path inside the Bitbucket repo that contains the app source
_APP_FILE = "infra-app/app.py"


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

    # Always include live probe results
    context_parts.append(
        f"LIVE SERVICE PROBE: {service_url} -> HTTP {live_status}\n"
        f"Response body: {live_body}"
    )

    enriched_logs = "\n\n".join(context_parts)

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
# Code fix path (push to Bitbucket → pipeline → Cloud Run)
# ---------------------------------------------------------------------------

async def _code_fix(incident: dict) -> dict:
    """
    Flow:
      1. Fetch current app.py from Bitbucket
      2. Ask AI to generate a patched version
      3. Commit the patch to Bitbucket
      4. Wait for the Bitbucket pipeline to finish (build + push image)
      5. Wait for Cloud Run to pick up the new revision
      6. Health-check; rollback (revert file + re-pipeline) if unhealthy
    """
    update_incident(incident["id"], {"status": "fetching_code"})

    # 1. Get current file
    try:
        current_content = await bitbucket.get_file(_APP_FILE)
    except Exception as exc:
        update_incident(incident["id"], {
            "status": "bitbucket_error",
            "bitbucket_error": f"Could not fetch file: {exc}",
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
    committed = await bitbucket.commit_file(_APP_FILE, fixed_content, commit_msg)
    if not committed:
        update_incident(incident["id"], {"status": "bitbucket_commit_failed"})
        return {"healthy": False, "error": "Bitbucket commit failed"}

    # 4. The commit triggers the pipeline automatically; wait for it
    update_incident(incident["id"], {"status": "waiting_pipeline"})
    pipeline_uuid = await bitbucket.trigger_pipeline()
    pipeline_ok = await bitbucket.wait_for_pipeline(pipeline_uuid, timeout=600)

    if not pipeline_ok:
        update_incident(incident["id"], {
            "status": "pipeline_failed",
            "pipeline_uuid": pipeline_uuid,
        })
        # Attempt rollback: restore previous content and re-trigger
        await bitbucket.commit_file(
            _APP_FILE, current_content, "revert: pipeline failed, restoring previous code"
        )
        rollback_uuid = await bitbucket.trigger_pipeline()
        await bitbucket.wait_for_pipeline(rollback_uuid, timeout=600)
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
        await bitbucket.commit_file(
            _APP_FILE, current_content, "revert: unhealthy after code fix, restoring previous code"
        )
        rollback_uuid = await bitbucket.trigger_pipeline()
        await bitbucket.wait_for_pipeline(rollback_uuid, timeout=600)
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


async def reject(incident_id: str):
    update_incident(incident_id, {"status": "rejected"})
