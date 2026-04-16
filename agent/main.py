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


# ── Additional simulate endpoints (infra + app variants) ─────────────────────

def _bg_alert(background: BackgroundTasks, source: str, service_url: str, body: dict):
    """Helper: run handle_alert + send_alert + send_deep_report in background."""
    async def run():
        try:
            incident = await handle_alert(source, service_url, alert_body=body)
            await send_alert(incident)
            await send_deep_report(incident)
        except Exception as exc:
            import traceback
            print(f"[ERROR] {source} failed: {exc}")
            traceback.print_exc()
    background.add_task(run)


@api.post("/simulate/infra/cpu")
async def simulate_infra_cpu(req: SimulateRequest, background: BackgroundTasks):
    """Simulate CPU throttling — container CPU usage sustained above 80%."""
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate", "type": "infra",
        "alertname": "HighCPUThrottling",
        "metric": "container_cpu_usage_seconds_total",
        "value": "87%",
        "threshold": "80%",
        "description": "CPU throttled at 87% for over 5 minutes — requests queuing up",
        "severity": "high",
        "service_url": service_url,
        "notes": req.notes,
    }
    _bg_alert(background, "simulate_infra_cpu", service_url, body)
    return {"status": "triggered", "type": "infra/cpu", "message": "CPU throttling simulation started — check Telegram"}


@api.post("/simulate/infra/crash")
async def simulate_infra_crash(req: SimulateRequest, background: BackgroundTasks):
    """Simulate container restart loop — multiple OOMKilled / crash restarts."""
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate", "type": "infra",
        "alertname": "ContainerRestartLoop",
        "metric": "container_restart_count",
        "value": "5 restarts in 10m",
        "threshold": "2 restarts in 10m",
        "description": "Container restarted 5 times in last 10 minutes — possible OOMKilled or crash loop",
        "severity": "critical",
        "events": ["OOMKilled", "BackOff", "CrashLoopBackOff"],
        "service_url": service_url,
        "notes": req.notes,
    }
    _bg_alert(background, "simulate_infra_crash", service_url, body)
    return {"status": "triggered", "type": "infra/crash", "message": "Crash loop simulation started — check Telegram"}


@api.post("/simulate/infra/network")
async def simulate_infra_network(req: SimulateRequest, background: BackgroundTasks):
    """Simulate network timeout — upstream connectivity failure between services."""
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate", "type": "infra",
        "alertname": "NetworkConnectivityFailure",
        "metric": "upstream_connect_error_rate",
        "value": "34% of requests failing",
        "threshold": "5%",
        "description": "34% of upstream connections timing out — possible VPC or DNS issue",
        "severity": "critical",
        "errors": ["upstream connect error", "connection timeout", "ECONNREFUSED"],
        "service_url": service_url,
        "notes": req.notes,
    }
    _bg_alert(background, "simulate_infra_network", service_url, body)
    return {"status": "triggered", "type": "infra/network", "message": "Network failure simulation started — check Telegram"}


@api.post("/simulate/app/errors")
async def simulate_app_errors(req: SimulateRequest, background: BackgroundTasks):
    """Simulate high 5xx error rate — application throwing unhandled exceptions."""
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate", "type": "app",
        "alertname": "HighErrorRate",
        "metric": "http_errors_total / http_requests_total",
        "value": "28% error rate",
        "threshold": "5%",
        "description": "28% of requests returning HTTP 500 — application throwing unhandled exceptions",
        "severity": "critical",
        "sample_errors": [
            "ERROR ZeroDivisionError: division by zero in /orders",
            "ERROR KeyError: 'user_id' in process_order()",
            "500 Internal Server Error x47 in last 5 minutes",
        ],
        "service_url": service_url,
        "notes": req.notes,
    }
    _bg_alert(background, "simulate_app_errors", service_url, body)
    return {"status": "triggered", "type": "app/errors", "message": "Error rate simulation started — check Telegram"}


@api.post("/simulate/app/leak")
async def simulate_app_leak(req: SimulateRequest, background: BackgroundTasks):
    """Simulate memory leak — gradual memory growth without release."""
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate", "type": "app",
        "alertname": "MemoryLeakDetected",
        "metric": "app_memory_usage_bytes (trend)",
        "value": "Memory grew from 120Mi → 380Mi over 2 hours without spike",
        "threshold": "256Mi",
        "description": "Memory increasing monotonically — no traffic spike, no batch job. Likely unbounded cache or accumulating object references.",
        "severity": "high",
        "pattern": "gradual_increase",
        "memory_samples": ["T+0min: 120Mi", "T+30min: 180Mi", "T+60min: 240Mi", "T+90min: 310Mi", "T+120min: 380Mi"],
        "service_url": service_url,
        "notes": req.notes,
    }
    _bg_alert(background, "simulate_app_leak", service_url, body)
    return {"status": "triggered", "type": "app/leak", "message": "Memory leak simulation started — check Telegram"}


@api.post("/simulate/app/db")
async def simulate_app_db(req: SimulateRequest, background: BackgroundTasks):
    """Simulate database connection exhaustion — all DB connections consumed."""
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    body = {
        "source": "simulate", "type": "app",
        "alertname": "DatabaseConnectionExhausted",
        "metric": "db_connection_pool_active",
        "value": "100/100 connections active (pool full)",
        "threshold": "80/100 connections",
        "description": "All DB connections consumed — new requests queueing or failing with connection timeout",
        "severity": "critical",
        "sample_errors": [
            "ERROR connection pool exhausted after 30s wait",
            "ERROR could not connect to database: too many clients",
            "WARN query took 28s waiting for connection slot",
        ],
        "service_url": service_url,
        "notes": req.notes,
    }
    _bg_alert(background, "simulate_app_db", service_url, body)
    return {"status": "triggered", "type": "app/db", "message": "DB exhaustion simulation started — check Telegram"}


# ── Correlate endpoint — cross-layer root cause analysis ─────────────────────

class CorrelateRequest(BaseModel):
    infra_logs: Optional[str] = ""
    app_logs: Optional[str] = ""
    business_logs: Optional[str] = ""
    metrics: Optional[dict] = {}
    config: Optional[dict] = {}
    send_to_telegram: Optional[bool] = False


@api.post("/correlate")
async def correlate(req: CorrelateRequest):
    """
    Correlate infrastructure logs + application logs + business logs together.
    Finds the TRUE root cause chain across all three layers.
    Returns a structured correlation report.

    Example:
      infra_logs:     "OOMKilled, container restarted 3 times"
      app_logs:       "500 errors on /orders, ZeroDivisionError"
      business_logs:  "45 orders failed to process, payment webhook timed out"
      metrics:        {"memory": "490Mi", "error_rate": "28%", "cpu": "45%"}
    """
    from ai import correlate_signals
    from cloudrun import get_config

    config = req.config or await get_config()

    report = await correlate_signals(
        infra_logs=req.infra_logs,
        app_logs=req.app_logs,
        business_logs=req.business_logs,
        metrics=req.metrics or {},
        config=config,
    )

    if req.send_to_telegram:
        await _send_correlation_report(report)

    return report


async def _send_correlation_report(report: dict):
    """Send correlation report to Telegram."""
    from telegram_bot import bot, _esc
    from config import settings

    def _e(val) -> str:
        return _esc(str(val)) if val else "N/A"

    chain = "\n".join(
        f"  {i+1}\\. {_e(s)}" for i, s in enumerate(report.get("causal_chain") or [])
    )
    inf_ev = "\n".join(f"  • {_e(e)}" for e in (report.get("infra_evidence") or []))
    app_ev = "\n".join(f"  • {_e(e)}" for e in (report.get("app_evidence") or []))
    biz_ev = "\n".join(f"  • {_e(e)}" for e in (report.get("business_evidence") or []))
    prev = "\n".join(f"  • {_e(p)}" for p in (report.get("prevention") or []))

    conf = report.get("confidence", "?")
    conf_icon = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(conf, "⚪")
    root_icon = {"infrastructure": "🏗️", "application": "🐛", "both": "⚡"}.get(
        report.get("root_layer", ""), "🔍"
    )

    text = (
        f"🔗 *Cross\\-Layer Correlation Report*\n\n"
        f"*Root Layer:* {root_icon} {_e(report.get('root_layer')).upper()}\n"
        f"*Infra Issue:* {_e(report.get('infra_issue'))}\n"
        f"*App Issue:* {_e(report.get('app_issue'))}\n\n"
        f"*Root Cause:*\n{_e(report.get('root_cause'))}\n\n"
        f"*Causal Chain:*\n{chain}\n\n"
        f"*Correlation Insight:*\n_{_e(report.get('correlation_insight'))}_\n\n"
        f"*Evidence — Infra:*\n{inf_ev}\n\n"
        f"*Evidence — Application:*\n{app_ev}\n\n"
        f"*Evidence — Business:*\n{biz_ev}\n\n"
        f"*Business Impact:*\n{_e(report.get('business_impact'))}\n\n"
        f"*Fix:*\n"
        f"  ⚡ Immediate: {_e(report.get('immediate_fix'))}\n"
        f"  🔧 Long\\-term: {_e(report.get('longterm_fix'))}\n\n"
        f"*Prevention:*\n{prev}\n\n"
        f"*Confidence:* {conf_icon} {conf} — {_e(report.get('confidence_reason'))}"
    )

    try:
        await bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
        )
    except Exception as exc:
        print(f"[ERROR] _send_correlation_report: {exc}")

