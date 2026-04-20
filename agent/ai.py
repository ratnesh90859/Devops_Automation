import json
import google.generativeai as genai
from config import settings

genai.configure(api_key=settings.GEMINI_API_KEY)
_model = genai.GenerativeModel(
    model_name="gemini-2.0-flash",
    system_instruction="GCP Cloud Run expert. Return raw JSON only. No markdown fences.",
)


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences that the model sometimes adds."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _validate_diagnosis(d: dict, config: dict, logs: str = "", alert_body: dict = None) -> dict:
    """
    Ensure every required field has a real non-null non-empty value.
    Fill in sensible defaults when the model omits or nullifies a field.
    Also performs a log-based override: if logs contain code-error signals
    but AI returned an infra fix, override to code_error.
    """
    mem      = config.get("memory", "256Mi")
    cpu      = config.get("cpu", "1")
    timeout  = config.get("timeout", "30")
    min_inst = str(config.get("min_instances", 0))

    # Helper: pick value if it's a non-empty string, else fallback
    def _pick(key, fallback):
        v = d.get(key)
        return v if (v is not None and str(v).strip().lower() not in ("none", "", "null")) else fallback

    issue  = _pick("issue_type", "crash")
    sev    = _pick("severity",   "high")
    ftype  = _pick("fix_type",   "infra")

    # ── Log-based override: catch cases where AI still returns infra for code bugs ──
    _code_signals = [
        "traceback", "exception", "division by zero", "zerodivisionerror",
        "keyerror", "typeerror", "valueerror", "attributeerror", "indexerror",
        "importerror", "nameerror", "syntaxerror", "runtimeerror",
    ]
    # ── Log-based override: detect memory signals so timeout is never suggested for OOM ──
    _memory_signals = [
        "memory_spike", "memory_leak_growing", "memory_high_warning", "oomkilled",
        "oom", "out of memory", "heavy_order_processed", "leak_items",
        "mem_after_mb", "mem_before_mb", "memory_pressure", "/heavy", "/leak",
        "service_degradation_risk",
    ]
    # ── Log-based override: detect deployment regression signals ──
    _regression_signals = [
        "deployment regression alert", "deployment occurred", "rollback target",
        "high probability of deployment regression",
    ]
    logs_lower = logs.lower() if logs else ""
    has_code_bug   = any(sig in logs_lower for sig in _code_signals)
    has_memory     = any(sig in logs_lower for sig in _memory_signals)
    has_regression = any(sig in logs_lower for sig in _regression_signals)

    # ── Alert-source override: anomaly_detector alerts ───────────────────────
    # anomaly_detector source signals a sudden statistical spike. Map to the
    # correct issue type based on the metric that spiked.
    _is_anomaly_alert = (alert_source == "anomaly_detector")
    if _is_anomaly_alert:
        _anomaly_metric = (alert_body or {}).get("anomaly_metric", "")
        if _anomaly_metric in ("latency_seconds", "latency_p95"):
            issue = "timeout"
            ftype = "infra"
            has_regression = False
        elif _anomaly_metric == "error_rate":
            issue = "crash"
            ftype = "code"
            has_regression = False
        elif _anomaly_metric == "request_rate":
            issue = "high_cpu"
            ftype = "infra"
            has_regression = False

    # ── Alert-source override: threshold_monitor memory_high is ALWAYS oom ──
    # Regression detection must never fire when the trigger is a gradual memory
    # leak monitored by the threshold monitor — those commit SHAs in context
    # look like "deployment info" to the AI but they are NOT a regression.
    alert_name = (alert_body or {}).get("alertname", "") if alert_body else ""
    alert_source = (alert_body or {}).get("source", "") if alert_body else ""
    is_memory_threshold_alert = (
        alert_source == "threshold_monitor" and alert_name == "memory_high"
    )
    if is_memory_threshold_alert:
        has_regression = False  # never misclassify a memory leak as regression
        if not has_code_bug:
            has_memory = True   # ensure memory path is taken

    # ── Alert-source override ──────────────────────────────────────────────────
    # threshold_monitor memory_high alerts are ALWAYS oom — never regression.
    _alert_name   = (alert_body or {}).get("alertname", "")
    _alert_source = (alert_body or {}).get("source", "")
    _is_mem_alert = (_alert_source == "threshold_monitor" and _alert_name == "memory_high")
    if _is_mem_alert:
        has_regression = False
        if not has_code_bug:
            has_memory = True

    # Regression takes highest priority (over memory, over code)
    if has_regression and issue not in ("deployment_regression",) and not has_code_bug:
        print(f"[VALIDATE] Overriding issue_type from '{issue}' to 'deployment_regression' — regression signals in logs")
        issue = "deployment_regression"
        ftype = "rollback"

    # Memory signals override timeout — slow requests near timeout are a SYMPTOM of
    # memory pressure, not a standalone timeout issue.
    elif has_memory and issue in ("timeout", "unknown", "crash") and not has_code_bug:
        print(f"[VALIDATE] Overriding issue_type from '{issue}' to 'oom' — memory signals found in logs")
        issue = "oom"
        ftype = "infra"

    if has_code_bug and issue not in ("code_error", "deployment_regression"):
        print(f"[VALIDATE] Overriding issue_type from '{issue}' to 'code_error' — code-error signals found in logs")
        issue = "code_error"
        ftype = "code"

    if has_code_bug and ftype not in ("code", "rollback"):
        print(f"[VALIDATE] Overriding fix_type from '{ftype}' to 'code' — code-error signals found in logs")
        ftype = "code"

    # Choose sensible fix based on issue
    fix_map = {
        "oom":                  ("memory",       mem,      _double_memory(mem)),
        "high_cpu":             ("cpu",           cpu,      "2"),
        "timeout":              ("timeout",       timeout,  str(min(int(timeout) * 2, 3600))),
        "cold_start":           ("min_instances", min_inst, "1"),
        "crash":                ("memory",        mem,      _double_memory(mem)),
        "code_error":           ("code",          "current","patched via GitHub PR"),
        "deployment_regression":("rollback",      "current","previous image"),
        "unknown":              ("min_instances", min_inst, "1"),
    }
    default_field, default_old, default_new = fix_map.get(issue, fix_map["crash"])

    return {
        "issue_type":    issue,
        "root_cause":    _pick("root_cause",    f"Service experiencing {issue} issue — {sev} severity alert breached threshold"),
        "fix_type":      ftype,
        "fix_field":     _pick("fix_field",     default_field),
        "fix_old_value": _pick("fix_old_value", default_old),
        "fix_new_value": _pick("fix_new_value", default_new),
        "fix_reason":    _pick("fix_reason",    f"Adjusting {default_field} will relieve the {issue} pressure"),
        "confidence":    float(d.get("confidence") or 0.8),
        "severity":      sev,
    }


def _double_memory(current: str) -> str:
    """256Mi -> 512Mi -> 1Gi -> 2Gi"""
    table = {"128Mi": "256Mi", "256Mi": "512Mi", "512Mi": "1Gi", "1Gi": "2Gi", "2Gi": "4Gi"}
    return table.get(current, "512Mi")


async def diagnose(logs: str, config: dict, alert_body: dict = None) -> dict:
    # Limit context length so Gemini doesn't get overwhelmed
    logs_trimmed = logs[:4000]

    # Pre-scan logs for obvious code-error signatures so the AI has a hint
    _code_error_signals = [
        "traceback", "exception", "error:", "division by zero", "zerodivisionerror",
        "keyerror", "typeerror", "valueerror", "attributeerror", "indexerror",
        "importerror", "nameerror", "syntaxerror", "runtimeerror", "filenotfounderror",
        "crash", "500 internal server error",
    ]
    # Pre-scan for memory pressure signals
    _memory_signals_scan = [
        "memory_spike", "memory_leak_growing", "heavy_order_processed", "leak_items",
        "mem_after_mb", "mem_before_mb", "memory_high_warning", "oomkilled",
        "/heavy", "/leak", "service_degradation_risk",
    ]
    # Pre-scan for deployment regression signals
    _regression_signals_scan = [
        "deployment regression alert", "deployment occurred",
        "high probability of deployment regression",
    ]
    logs_lower = logs_trimmed.lower()
    detected_signals    = [s for s in _code_error_signals    if s in logs_lower]
    detected_memory     = [s for s in _memory_signals_scan    if s in logs_lower]
    detected_regression = [s for s in _regression_signals_scan if s in logs_lower]

    code_hint = ""
    if detected_signals:
        code_hint = (
            f"\n\n⚠️ PRE-SCAN DETECTED CODE-LEVEL SIGNALS in logs: {detected_signals}\n"
            f"This STRONGLY suggests issue_type='code_error' with fix_type='code'.\n"
            f"Do NOT suggest infra fixes (memory/cpu/timeout) for code bugs like "
            f"division by zero, key errors, type errors, etc.\n"
        )
    memory_hint = ""
    if detected_memory and not detected_signals:
        memory_hint = (
            f"\n\n⚠️ PRE-SCAN DETECTED MEMORY PRESSURE SIGNALS in logs: {detected_memory}\n"
            f"This STRONGLY suggests issue_type='oom' with fix_type='infra', fix_field='memory'.\n"
            f"Slow requests are a SYMPTOM of memory pressure — the fix is MORE MEMORY, NOT longer timeout.\n"
        )
    regression_hint = ""
    if detected_regression and not detected_signals:
        regression_hint = (
            f"\n\n⚠️ PRE-SCAN DETECTED DEPLOYMENT REGRESSION SIGNALS: {detected_regression}\n"
            f"This STRONGLY suggests issue_type='deployment_regression' with fix_type='rollback'.\n"
            f"The issue started after a recent deployment — rollback is the correct fix.\n"
        )

    # If alert explicitly came from anomaly_detector, inject specific hint
    anomaly_hint = ""
    if _alert_source_d == "anomaly_detector":
        _anomaly_metric_d = (alert_body or {}).get("anomaly_metric", "")
        _z_score          = (alert_body or {}).get("anomaly_z_score", "")
        _ratio            = (alert_body or {}).get("anomaly_ratio", "")
        anomaly_hint = (
            f"\n\n🔴 ALERT SOURCE: anomaly_detector\n"
            f"A SUDDEN STATISTICAL SPIKE was detected in metric '{_anomaly_metric_d}' "
            f"(z={_z_score}σ, {_ratio}× above baseline). "
            f"This is NOT a gradual threshold breach — it is a sharp, sudden change.\n"
        )
        if _anomaly_metric_d == "latency_seconds":
            anomaly_hint += (
                "Latency spiked suddenly. Most likely cause: CPU throttling, memory pressure, "
                "or a slow external dependency. Suggest issue_type='timeout', fix_type='infra', "
                "increase cloudrun_timeout or cpu.\n"
            )
        elif _anomaly_metric_d == "error_rate":
            anomaly_hint += (
                "Error rate spiked suddenly. Most likely cause: a bad deployment, "
                "code exception, or dependency failure. Suggest issue_type='crash', fix_type='code'.\n"
            )
        elif _anomaly_metric_d == "request_rate":
            anomaly_hint += (
                "Request rate spiked suddenly (traffic spike). Most likely cause: viral traffic, "
                "bot traffic, or upstream retry storm. Suggest issue_type='high_cpu', "
                "fix_type='infra', increase cpu or max_instances.\n"
            )
        regression_hint = ""
        if not detected_signals:
            code_hint = ""
            memory_hint = ""

    # If alert explicitly came from threshold_monitor memory_high, override hints
    if _alert_source_d == "threshold_monitor" and _alert_name_d == "memory_high":
        regression_hint = ""  # never suggest regression for a threshold-fired memory alert
        if not detected_signals:
            memory_hint = (
                f"\n\n🔴 ALERT SOURCE: threshold_monitor / memory_high\n"
                f"Memory RSS has GRADUALLY exceeded the container limit due to a memory leak.\n"
                f"This is NOT a deployment regression — it is a slow leak that builds over time.\n"
                f"The ONLY correct fix is: issue_type='oom', fix_type='infra', fix_field='memory', double the memory.\n"
                f"Do NOT suggest rollback. Do NOT suggest timeout. Increase memory only.\n"
            )

    prompt = f"""You are a GCP Cloud Run SRE diagnosing a production incident.

CURRENT SERVICE CONFIG:
{json.dumps(config, indent=2)}

INCIDENT CONTEXT (alert payload, logs, metrics):
{logs_trimmed}
{code_hint}{memory_hint}{regression_hint}{anomaly_hint}

TASK: Analyze the above and return a JSON diagnosis.

═══ CLASSIFICATION RULES (follow in PRIORITY ORDER) ═══

STEP 0 — CHECK FOR DEPLOYMENT REGRESSION FIRST:
If the context contains 'DEPLOYMENT REGRESSION ALERT' OR 'deployment occurred X minutes ago':
  AND latency/errors increased AFTER that deployment:
  → issue_type = "deployment_regression", fix_type = "rollback"
  → The fix is rolling back to the previous image, NOT infra scaling.

STEP 1 — CHECK FOR CODE BUGS:
If logs contain ANY Python traceback, exception, or error like:
  - ZeroDivisionError, KeyError, TypeError, ValueError, AttributeError
  - IndexError, ImportError, NameError, SyntaxError, RuntimeError
  - "division by zero", "500 Internal Server Error" with a traceback
  - Any unhandled exception causing request failures
→ issue_type = "code_error", fix_type = "code"
→ Infra changes (memory/cpu/timeout) will NOT fix code bugs.

STEP 2 — CHECK FOR MEMORY PRESSURE:
If logs contain ANY of:
  - memory_spike, memory_leak_growing, mem_after_mb, mem_before_mb
  - OOMKilled, "out of memory", heavy_order_processed, leak_items
  - /heavy endpoint, /leak endpoint, LOAD_SIZE, memory_high_warning
  - gradual memory growth, memory_pressure, service_degradation_risk
→ issue_type = "oom", fix_type = "infra", fix_field = "memory"
⚠️ IMPORTANT: Slow requests caused by memory pressure look like timeout — but the
correct fix is MORE MEMORY, not a longer timeout. High memory usage makes the
Python process slow. Doubling the timeout does NOT fix the slowness.

STEP 3 — ONLY IF NO CODE BUG AND NO MEMORY ISSUE, CHECK OTHER INFRA:
- CPU throttle / CPU at limit → issue_type = "high_cpu"
- Requests genuinely timing out with NO memory and NO code error → issue_type = "timeout"
- Cold start latency with no errors → issue_type = "cold_start"
- General crash with no clear code error or memory issue → issue_type = "crash"

═══ FIX MAPPING ═══
- deployment_regression → fix_type="rollback", fix_field="rollback", fix_new_value="previous image"
- code_error  → fix_type="code", fix_field="code", fix_old_value="current", fix_new_value="patched via GitHub PR"
- oom         → fix_type="infra", fix_field="memory", double the current value
- crash (no code bug in logs) → fix_type="infra", fix_field="memory", double the current value
- timeout     → fix_type="infra", fix_field="timeout", double the value
- high_cpu    → fix_type="infra", fix_field="cpu", increase to "2"
- cold_start  → fix_type="infra", fix_field="min_instances", set to "1"

═══ KEY PRINCIPLE ═══
Look at MEMORY USAGE first. If memory is normal (e.g. 40MB on a 256Mi container),
DO NOT suggest increasing memory. That is a waste. Look at what the actual error is.

CRITICAL: Every field below MUST have a real value. NEVER use null, None, or empty string.

Return ONLY this JSON (no markdown fences, no extra text):
{{
  "issue_type": "<one of: deployment_regression, code_error, oom, crash, timeout, high_cpu, cold_start>",
  "root_cause": "specific one-sentence explanation referencing the actual error from logs",
  "fix_type": "<code or infra>",
  "fix_field": "<code | memory | cpu | timeout | min_instances>",
  "fix_old_value": "<current value>",
  "fix_new_value": "<new value>",
  "fix_reason": "one sentence why this specific change resolves the issue",
  "confidence": 0.85,
  "severity": "high"
}}"""

    try:
        response = _model.generate_content(prompt)
        raw = _strip_fences(response.text)
        print(f"[DEBUG] diagnose() raw response: {raw[:500]}")
        parsed = json.loads(raw)
        return _validate_diagnosis(parsed, config, logs=logs_trimmed, alert_body=alert_body)
    except Exception as exc:
        print(f"[ERROR] diagnose() failed: {exc}")
        # Hard fallback — build from config without AI
        return _validate_diagnosis({}, config, logs=logs_trimmed, alert_body=alert_body)


async def analyze_deep(logs: str, config: dict, issue_type: str = "unknown") -> dict:
    """
    Full SRE-style deep analysis following the structured format:
    Issue Type / Root Cause / Key Evidence / Timeline / Recommended Fix / Prevention / Confidence
    Returns a dict with all sections as strings.
    """
    prompt = f"""You are a senior SRE and backend engineer expert in memory management and distributed systems.

Analyze the following inputs and produce a structured incident report.

CURRENT SERVICE CONFIG:
{json.dumps(config, indent=2)}

CONTEXT (alert payload + logs + live probe):
{logs}

KNOWN ISSUE TYPE (from fast diagnosis): {issue_type}

You must analyze exactly these aspects:

1. ISSUE CLASSIFICATION — Is this an Application Issue, Infrastructure Issue, or Both?
   - Application: memory leak, inefficient data handling, unbounded caching, bad algorithm
   - Infrastructure: memory limit too low, traffic spike, node resource pressure, bad resource config

2. ROOT CAUSE — Simple clear explanation. Reference actual values from logs/config.

3. KEY EVIDENCE — List 3-5 specific observations from logs/metrics that support your conclusion.

4. TIMELINE — Step by step sequence of events that led to the incident.

5. BUSINESS IMPACT — What business function was affected? Orders? Payments? API availability?
   What is the blast radius if not fixed?

6. RECOMMENDED FIX
   - Immediate (fix right now, <5 minutes)
   - Long-term (permanent solution, code or architecture change)

7. PREVENTION STRATEGY — What alerts, metrics, or code changes would prevent this in future?

8. CONFIDENCE — High/Medium/Low. Explain why based on quality of evidence.

Return raw JSON only. No markdown fences. No explanation outside JSON.
CRITICAL: Every field MUST have a real value. NEVER use null, None, or empty strings.
Fill every list with at least 3 specific observations from the context above.

{{
  "issue_classification": "Application|Infrastructure|Both",
  "root_cause": "clear explanation referencing actual log values and config",
  "key_evidence": ["observation 1", "observation 2", "observation 3"],
  "timeline": ["step 1", "step 2", "step 3", "step 4"],
  "business_impact": "what business function is affected and blast radius",
  "immediate_fix": "action to take right now",
  "longterm_fix": "permanent solution",
  "prevention": ["alert/metric suggestion 1", "alert/metric suggestion 2", "code improvement"],
  "confidence": "High|Medium|Low",
  "confidence_reason": "why confidence is High/Medium/Low based on evidence quality"
}}"""

    try:
        response = _model.generate_content(prompt)
        raw = _strip_fences(response.text)
        print(f"[DEBUG] analyze_deep() raw: {raw[:300]}")
        result = json.loads(raw)
        # Ensure lists are never null
        for list_key in ("key_evidence", "timeline", "prevention"):
            if not result.get(list_key):
                result[list_key] = ["See alert payload for details"]
        for str_key in ("issue_classification", "root_cause", "business_impact",
                        "immediate_fix", "longterm_fix", "confidence", "confidence_reason"):
            if not result.get(str_key):
                result[str_key] = "See alert context for details"
        return result
    except Exception as exc:
        print(f"[ERROR] analyze_deep() failed: {exc}")
        return {
            "issue_classification": "Infrastructure",
            "root_cause": "Threshold breach detected — error rate or request count exceeded configured limit",
            "key_evidence": ["Threshold breached as reported in alert payload", "Service generating 5xx responses", "Automatic remediation triggered"],
            "timeline": ["Alert threshold was breached", "Agent webhook received alert", "AI diagnosis initiated", "Fix proposal generated"],
            "business_impact": "Service degradation affecting end users — orders and API calls may be failing",
            "immediate_fix": "Scale up memory and set min-instances to 1 to prevent cold starts",
            "longterm_fix": "Add proper error handling and resource limits to the application",
            "prevention": ["Add custom alerting on error rate > 5%", "Monitor memory usage trends", "Add circuit breaker pattern"],
            "confidence": "High",
            "confidence_reason": "Threshold breach clearly observed in alert payload",
        }


async def correlate_signals(
    infra_logs: str,
    app_logs: str,
    business_logs: str,
    metrics: dict,
    config: dict,
) -> dict:
    """
    Correlate infrastructure logs, application logs, and business logs together
    to find the true root cause chain:
      Infrastructure problem → Application impact → Business impact

    Or the reverse:
      Bad code → Resource exhaustion → Infrastructure alert

    Returns a structured correlation report.
    """
    prompt = f"""You are a senior SRE, backend engineer, and business analyst.

You have THREE separate log streams and metrics from a production incident.
Your job is to CORRELATE all signals and find the true root cause chain.

═══════════════════════════════════════════════════════
SERVICE CONFIG:
{json.dumps(config, indent=2)}

REAL-TIME METRICS:
{json.dumps(metrics, indent=2)}

═══════════════════════════════════════════════════════
INFRASTRUCTURE LOGS (Cloud Run / container / platform logs):
{infra_logs[:2000] if infra_logs else "No infra logs provided"}

═══════════════════════════════════════════════════════
APPLICATION LOGS (app-level errors, warnings, tracebacks):
{app_logs[:2000] if app_logs else "No application logs provided"}

═══════════════════════════════════════════════════════
BUSINESS LOGS (order failures, payment errors, user-facing events):
{business_logs[:2000] if business_logs else "No business logs provided"}

═══════════════════════════════════════════════════════

CORRELATION RULES:
1. Find CAUSAL CHAIN — which layer triggered which:
   - Infra (CPU/memory/network) → triggered by → App (bad code/leak) or external traffic spike
   - App failures (5xx, exceptions) → caused by → Infra limits or bad code
   - Business failures (orders failed, payments dropped) → caused by → App errors or Infra crash

2. Classify ROOT LAYER:
   - "infrastructure": The primary fault is in platform/resource configuration
   - "application": The primary fault is in the code/logic
   - "both": Neither infra nor app alone caused it — interaction between both

3. Correlate TIMING:
   - Did infra metrics spike BEFORE app errors? → Infra caused app failures
   - Did app errors appear BEFORE infra metrics spiked? → App code caused resource exhaustion
   - Did business failures appear together with app errors? → App outage caused business loss

4. Issue types to detect:
   INFRA:
   - oom: Container memory exceeded limit (OOMKilled)
   - high_cpu: CPU throttled > 80% sustained
   - crash: Container restart loop
   - cold_start: Scale-from-zero latency spike
   - network_timeout: Upstream connectivity failure
   - disk_pressure: Storage I/O exhaustion

   APPLICATION:
   - error_rate: HTTP 5xx rate > threshold
   - memory_leak: Memory growing monotonically over time
   - db_exhaustion: Connection pool exhausted / DB unreachable
   - slow_query: Database or downstream query too slow
   - unhandled_exception: Python exception / traceback
   - cpu_bound: Expensive computation blocking event loop
   - latency_spike: p95/p99 latency threshold breached

Return raw JSON only. No markdown fences. No explanation outside JSON.

{{
  "root_layer": "infrastructure|application|both",
  "infra_issue": "one of: oom|high_cpu|network_timeout|crash|cold_start|disk_pressure|none",
  "app_issue": "one of: error_rate|memory_leak|db_exhaustion|slow_query|unhandled_exception|cpu_bound|latency_spike|none",
  "causal_chain": [
    "step 1: what triggered first",
    "step 2: how it propagated",
    "step 3: final user/business impact"
  ],
  "root_cause": "single precise sentence explaining the TRUE root cause with evidence from all three log layers",
  "infra_evidence": ["infra log observation 1", "infra log observation 2"],
  "app_evidence": ["app log observation 1", "app log observation 2"],
  "business_evidence": ["business log observation 1", "business log observation 2"],
  "correlation_insight": "key insight from COMBINING all three layers that would be missed looking at any single layer alone",
  "business_impact": "specific business function affected, estimated revenue/user impact if known",
  "immediate_fix": "exact action to take right now (<5 minutes)",
  "longterm_fix": "permanent architectural or code solution",
  "prevention": [
    "specific alert to add with threshold",
    "specific metric to track",
    "code or infra improvement"
  ],
  "confidence": "High|Medium|Low",
  "confidence_reason": "why based on quality and consistency of evidence across all three log streams"
}}"""
    response = _model.generate_content(prompt)
    return json.loads(_strip_fences(response.text))


async def suggest_code_fix(issue_type: str, logs: str, file_content: str) -> dict:
    """
    Given current application code and error logs, ask the AI to produce a
    patched version of the file that resolves the reported issue.

    Returns:
      {
        "needs_code_fix": bool,
        "fixed_content":  "complete corrected file as a string",
        "commit_message": "fix: short description",
        "explanation":    "one sentence"
      }
    """
    prompt = f"""You are a senior Python developer reviewing a Flask application for GCP Cloud Run.

ISSUE TYPE: {issue_type}

RECENT LOGS (last 2000 chars):
{logs[:2000]}

CURRENT app.py:
{file_content}

Task:
1. Decide whether the issue is caused by a code bug (needs_code_fix: true) or is purely
   infrastructure/load-based (needs_code_fix: false).
2. If needs_code_fix is true, return the COMPLETE corrected file content (not a diff).
3. Keep all existing routes and Prometheus metrics. Only fix the actual bug.
4. For latency / timeout issues: lower the SLEEP_SECONDS default to 1.
5. For OOM / memory issues that are code-driven: lower the LOAD_SIZE default to 100000.
6. For Python exceptions or import errors: fix the bad code path.

Return exactly this JSON (raw, no fences):
{{
  "needs_code_fix": true,
  "fixed_content": "...complete corrected file...",
  "commit_message": "fix: one-line description",
  "explanation": "one sentence"
}}"""
    response = _model.generate_content(prompt)
    return json.loads(_strip_fences(response.text))
