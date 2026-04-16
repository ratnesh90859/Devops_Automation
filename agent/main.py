from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from telegram import Update
from telegram_bot import send_alert, send_deep_report, setup as tg_setup, tg_app
from flow import handle_alert
from db import list_incidents
from config import settings

api = FastAPI(title="Infra AI Debugger", version="1.0.0")


@api.on_event("startup")
async def startup():
    await tg_setup()


@api.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    if request.headers.get("X-Token") != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    source = body.get("source", "grafana")

    # ------------------------------------------------------------------
    # Bitbucket pipeline success notification
    # The pipeline posts here after a successful deploy so the agent knows
    # the new image is live.  The agent just logs/acknowledges it; any
    # pending health-check is already handled inside flow.execute_fix().
    # ------------------------------------------------------------------
    if source == "bitbucket" and body.get("status") == "success":
        return {
            "received": True,
            "message": "pipeline success acknowledged",
            "build_number": body.get("build_number"),
            "commit": body.get("commit"),
        }

    # ------------------------------------------------------------------
    # Bitbucket pipeline failure OR Grafana alert → trigger AI diagnosis
    # ------------------------------------------------------------------
    service_url = body.get("service_url", settings.CLOUD_RUN_SERVICE_URL)

    async def run():
        try:
            incident = await handle_alert(source, service_url, alert_body=body)
            await send_alert(incident)
            await send_deep_report(incident)
        except Exception as exc:
            import traceback
            print(f"[ERROR] background alert failed: {exc}")
            traceback.print_exc()

    background.add_task(run)
    return {"received": True}


@api.post("/telegram")
async def telegram_webhook(request: Request):
    body = await request.json()
    update = Update.de_json(body, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


@api.get("/incidents")
async def incidents():
    return list_incidents()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/debug/terraform")
async def debug_terraform():
    import os
    tf_dir = settings.TERRAFORM_DIR
    exists = os.path.isdir(tf_dir)
    files = os.listdir(tf_dir) if exists else []
    tfvars_path = os.path.join(tf_dir, "terraform.tfvars")
    tfvars_exists = os.path.isfile(tfvars_path)
    tfvars_content = ""
    if tfvars_exists:
        with open(tfvars_path) as f:
            tfvars_content = f.read()
    return {
        "terraform_dir": tf_dir,
        "dir_exists": exists,
        "files": files,
        "tfvars_exists": tfvars_exists,
        "tfvars_content": tfvars_content,
    }


# ── Simulate endpoints ────────────────────────────────────────────────────────

class SimulateRequest(BaseModel):
    service_url: Optional[str] = None
    notes: Optional[str] = ""


@api.post("/simulate/infra")
async def simulate_infra(req: SimulateRequest, background: BackgroundTasks):
    """
    Simulate an infrastructure issue (OOM / memory spike).
    Triggers full AI diagnosis + deep SRE report + Telegram alert.
    """
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate",
        "type": "infra",
        "alertname": "HighMemoryUsage",
        "metric": "container_memory_usage_bytes",
        "value": "490Mi",
        "threshold": "256Mi",
        "description": "Memory usage exceeded limit — potential OOM kill imminent",
        "severity": "critical",
        "service_url": service_url,
        "notes": req.notes,
    }

    async def run():
        try:
            incident = await handle_alert("simulate_infra", service_url, alert_body=body)
            await send_alert(incident)
            await send_deep_report(incident)
        except Exception as exc:
            import traceback
            print(f"[ERROR] simulate_infra failed: {exc}")
            traceback.print_exc()

    background.add_task(run)
    return {"status": "triggered", "type": "infra", "message": "OOM simulation started — check Telegram"}


@api.post("/simulate/app")
async def simulate_app(req: SimulateRequest, background: BackgroundTasks):
    """
    Simulate an application issue (high latency / slow endpoint).
    Triggers full AI diagnosis + deep SRE report + Telegram alert.
    """
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate",
        "type": "app",
        "alertname": "HighLatency",
        "metric": "http_request_duration_seconds_p95",
        "value": "8.4s",
        "threshold": "2.0s",
        "description": "p95 request latency exceeds threshold — users experiencing slow responses",
        "severity": "high",
        "service_url": service_url,
        "notes": req.notes,
    }

    async def run():
        try:
            incident = await handle_alert("simulate_app", service_url, alert_body=body)
            await send_alert(incident)
            await send_deep_report(incident)
        except Exception as exc:
            import traceback
            print(f"[ERROR] simulate_app failed: {exc}")
            traceback.print_exc()

    background.add_task(run)
    return {"status": "triggered", "type": "app", "message": "Latency simulation started — check Telegram"}


# ── Standalone analyze endpoint ───────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    logs: str
    config: Optional[dict] = {}
    issue_type: Optional[str] = "unknown"
    send_to_telegram: Optional[bool] = False


@api.post("/analyze")
async def analyze(req: AnalyzeRequest):
    """
    Accept raw logs + config and return a full structured SRE deep analysis report.
    Optionally send the report to Telegram.
    """
    from ai import analyze_deep
    report = await analyze_deep(req.logs, req.config, req.issue_type)

    if req.send_to_telegram:
        fake_incident = {"deep_report": report}
        await send_deep_report(fake_incident)

    return {
        "issue_classification": report.get("issue_classification"),
        "root_cause": report.get("root_cause"),
        "key_evidence": report.get("key_evidence"),
        "timeline": report.get("timeline"),
        "business_impact": report.get("business_impact"),
        "immediate_fix": report.get("immediate_fix"),
        "longterm_fix": report.get("longterm_fix"),
        "prevention": report.get("prevention"),
        "confidence": report.get("confidence"),
        "confidence_reason": report.get("confidence_reason"),
    }
