from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from telegram import Update
from telegram_bot import send_alert, send_deep_report, setup as tg_setup, tg_app
from flow import handle_alert
from db import list_incidents
from config import settings
import terraform_runner

api = FastAPI(title="Infra AI Debugger", version="1.0.0")


@api.on_event("startup")
async def startup():
    await tg_setup()


@api.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    body = await request.json()
    source = body.get("source", "grafana")

    # Grafana sends alerts without a secret token — identify it by payload shape
    # All other callers must provide X-Token header
    is_grafana = (
        "alerts" in body                   # Grafana unified alerting payload
        or source == "grafana"
        or "externalURL" in body           # Grafana includes this field
    )
    if not is_grafana:
        if request.headers.get("X-Token") != settings.WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ------------------------------------------------------------------
    # Bitbucket pipeline success notification
    # ------------------------------------------------------------------
    if source == "bitbucket" and body.get("status") == "success":
        # Track this deployment for regression detection.
        # The pipeline sends fix_meta (type/incident_id) + commit + image_tag.
        try:
            from db import track_deployment
            fix_meta  = body.get("fix_meta") or {}
            commit_id = body.get("commit", "unknown")
            image_tag = body.get("image_tag", commit_id)
            track_deployment(
                commit_id=commit_id,
                image_tag=image_tag,
                fix_type=fix_meta.get("fix_type", "app"),
                incident_id=fix_meta.get("incident_id", ""),
            )
            print(f"[INFO] Deployment tracked: commit={commit_id[:10]} fix_type={fix_meta.get('fix_type','app')}")
        except Exception as _dep_exc:
            print(f"[WARN] track_deployment failed: {_dep_exc}")

        return {
            "received": True,
            "message": "pipeline success acknowledged",
            "build_number": body.get("build_number"),
            "commit": body.get("commit"),
        }

    # ------------------------------------------------------------------
    # Grafana unified alerting payload — extract alert summary into body
    # Grafana sends: {"alerts": [...], "commonLabels": {...}, "externalURL": "..."}
    # ------------------------------------------------------------------
    if "alerts" in body:
        firing = [a for a in body["alerts"] if a.get("status") == "firing"]
        if not firing:
            return {"received": True, "message": "no firing alerts"}
        first = firing[0]
        # Re-shape into the format handle_alert / AI expects
        source = "grafana"
        body = {
            "source": "grafana",
            "alertname": first.get("labels", {}).get("alertname", ""),
            "severity":  first.get("labels", {}).get("severity", "high"),
            "service":   first.get("labels", {}).get("service", "order-api"),
            "summary":   first.get("annotations", {}).get("summary", ""),
            "description": first.get("annotations", {}).get("description", ""),
            "metric":    first.get("labels", {}).get("alertname", ""),
            "value":     (first.get("values") or {}).get("B", first.get("valueString", "")),
            "grafana_url": body.get("externalURL", ""),
        }

    # ------------------------------------------------------------------
    # Grafana alert OR Bitbucket failure → trigger AI diagnosis
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
    from telegram_bot import _safe_send, _esc
    def _e(val) -> str:
        return _esc(str(val)) if val else "N/A"

    chain = "\n".join(
        f"  {i+1}. {_e(s)}" for i, s in enumerate(report.get("causal_chain") or [])
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
        f"🔗 *Cross-Layer Correlation Report*\n\n"
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
        f"  🔧 Long-term: {_e(report.get('longterm_fix'))}\n\n"
        f"*Prevention:*\n{prev}\n\n"
        f"*Confidence:* {conf_icon} {conf} — {_e(report.get('confidence_reason'))}"
    )

    await _safe_send(text=text, parse_mode="Markdown")


# ── Demo (end-to-end auto flow) ───────────────────────────────────────────────
# POST /demo/start  → wakes up the infra-app load generator so requests build
#                     up from zero.  When the threshold is crossed the infra-app
#                     POSTs to /webhook here, which triggers AI diagnosis +
#                     Telegram alert + approval flow automatically.
# POST /demo/stop   → stop the load generator early
# GET  /demo/status → live counters + incident list

class DemoStartRequest(BaseModel):
    scenario:         Optional[str]   = "mixed"   # mixed | crash | memory | slow
    delay_secs:       Optional[float] = 0.3       # pause between fake requests
    req_threshold:    Optional[int]   = None      # override app default (100)
    error_threshold:  Optional[int]   = None      # override app default (10)
    service_url:      Optional[str]   = None      # infra-app base URL (defaults to CLOUD_RUN_SERVICE_URL)


@api.post("/demo/start")
async def demo_start(req: DemoStartRequest, background: BackgroundTasks):
    """
    One-click enterprise demo:

    1. Hits the infra-app /demo/start to launch the load generator
    2. The load generator fires real requests against its own endpoints
       (/orders, /heavy, /crash, /leak) — counters start from zero
    3. When total_requests OR total_errors hits its threshold, the
       infra-app POSTs an alert to this agent's /webhook
    4. The agent runs AI diagnosis (fast + deep) in parallel
    5. A Telegram message is sent:
         • Issue type, severity, confidence, root cause
         • Proposed fix (infra Terraform change or code push)
         • [✅ Apply Fix] / [❌ Reject] buttons
    6. You click ✅ and type your reason → fix is applied automatically
    7. After fix: post-mortem report with full cross-layer log correlation
    """
    import httpx
    service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL

    payload: dict = {
        "scenario":   req.scenario,
        "delay_secs": req.delay_secs,
        # tell infra-app to call back THIS agent (not the order-api itself)
        "target_url": settings.BASE_URL,
    }
    if req.req_threshold is not None:
        payload["req_threshold"]   = req.req_threshold
    if req.error_threshold is not None:
        payload["error_threshold"] = req.error_threshold

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{service_url}/demo/start",
                json=payload,
                headers={"X-Admin-Token": "infraguard-secret-2026"},
            )
            r.raise_for_status()
            app_response = r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not start demo on infra-app: {exc}")

    # Also notify Telegram that the demo has started
    async def _notify():
        try:
            from telegram_bot import _safe_send, _esc
            scenario = _esc(req.scenario or "mixed")
            rth       = _esc(str(app_response.get("req_threshold", "100")))
            eth       = _esc(str(app_response.get("error_threshold", "10")))
            delay     = _esc(str(req.delay_secs))
            await _safe_send(
                text=(
                    f"🚦 *Enterprise Demo Started*\n\n"
                    f"*Scenario:* `{scenario}`\n"
                    f"*Request threshold:* {rth} requests\n"
                    f"*Error threshold:* {eth} errors\n"
                    f"*Request delay:* {delay}s\n\n"
                    f"Counters are now at *zero* and climbing.\n"
                    f"When the threshold is breached the AI agent will:\n"
                    f"  1. Diagnose the issue\n"
                    f"  2. Send a fix proposal here\n"
                    f"  3. Wait for your approval + reason\n"
                    f"  4. Apply the fix automatically\n"
                    f"  5. Send a post-mortem correlation report\n\n"
                    f"_Watch this chat._"
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            print(f"[ERROR] demo_start notify: {exc}")

    background.add_task(_notify)

    return {
        "status":      "demo_started",
        "scenario":    req.scenario,
        "delay_secs":  req.delay_secs,
        "app_response": app_response,
        "message": (
            "Load generator active. "
            "Watch Telegram — alert will fire when threshold is breached."
        ),
    }


@api.post("/demo/stop")
async def demo_stop(req: SimulateRequest):
    """Stop the running load generator on the infra-app."""
    import httpx
    url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{url}/demo/stop",
                headers={"X-Admin-Token": "infraguard-secret-2026"},
            )
            return r.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not stop demo: {exc}")


@api.get("/demo/status")
async def demo_status(service_url: Optional[str] = None):
    """
    Live status: threshold counters + demo running state + recent incidents.
    Poll this every few seconds while the demo is running to watch counters climb.
    """
    import httpx
    url = service_url or settings.CLOUD_RUN_SERVICE_URL
    app_status = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{url}/demo/status")
            app_status = r.json()
    except Exception as exc:
        app_status = {"error": str(exc)}

    recent_incidents = []
    try:
        recent_incidents = list_incidents()[:5]
    except Exception:
        pass

    return {
        "app_load_generator": app_status,
        "recent_incidents":   recent_incidents,
    }


# ── Reset endpoint — restore everything to baseline for retesting ─────────────

class ResetRequest(BaseModel):
    reset_infra: Optional[bool] = True   # run terraform reset to baseline
    reset_app: Optional[bool] = True     # call /reset on the order-api app
    notify_telegram: Optional[bool] = True
    service_url: Optional[str] = None


@api.post("/reset")
async def reset_all(req: ResetRequest):
    """
    Reset EVERYTHING back to baseline so you can retest from a clean state.

    What it resets:
      INFRA (Terraform):
        - cloudrun_memory        → 256Mi
        - cloudrun_cpu           → 1
        - cloudrun_timeout       → 30
        - cloudrun_min_instances → 0
        - cloudrun_max_instances → 3

      APP (order-api /reset):
        - Clears memory leak store (_leak_store emptied, GC forced)
        - Re-enables /heavy endpoint
    """
    import httpx
    from telegram_bot import bot, _esc
    results = {}

    # ── 1. Reset app internal state ───────────────────────────────────────────
    if req.reset_app:
        service_url = req.service_url or settings.CLOUD_RUN_SERVICE_URL
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{service_url}/reset")
                results["app_reset"] = r.json()
        except Exception as exc:
            results["app_reset"] = {"error": str(exc)}

    # ── 2. Reset Terraform infra to baseline ──────────────────────────────────
    if req.reset_infra:
        ok, tf_output = await terraform_runner.reset_to_baseline()
        results["infra_reset"] = {
            "success": ok,
            "summary": tf_output,
        }
    else:
        ok = True

    # ── 3. Notify Telegram ────────────────────────────────────────────────────
    if req.notify_telegram:
        app_status = results.get("app_reset", {})
        infra_status = results.get("infra_reset", {})

        app_mem = app_status.get("memory_after_mb", "?")
        app_cleared = app_status.get("cleared_leak_items", 0)
        app_ok = "error" not in app_status
        infra_ok = infra_status.get("success", True)

        icon = "✅" if (app_ok and infra_ok) else "⚠️"
        text = (
            f"{icon} *System Reset Complete*\n\n"
            f"*App State:*\n"
            f"  • Leak store cleared: {app_cleared} items removed\n"
            f"  • Memory after GC: {app_mem} MB\n"
            f"  • /heavy re\\-enabled: ✅\n\n"
            f"*Infra Reset \\(Terraform\\):*\n"
            f"  • memory → 256Mi\n"
            f"  • cpu → 1\n"
            f"  • timeout → 30s\n"
            f"  • min\\_instances → 0\n"
            f"  • max\\_instances → 3\n\n"
            f"🔁 Ready to retest from scratch\\."
        )
        try:
            await bot.send_message(
                chat_id=settings.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="MarkdownV2",
            )
        except Exception as exc:
            print(f"[ERROR] reset notify: {exc}")

    return {
        "status": "reset_complete",
        "results": results,
        "message": "System back to baseline. You can now retest any scenario.",
    }

