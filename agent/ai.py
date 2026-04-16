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


async def diagnose(logs: str, config: dict) -> dict:
    prompt = f"""You are a GCP Cloud Run expert diagnosing production issues.
Read the alert payload, logs, live probe result, and current config. Return a JSON diagnosis.
Raw JSON only. No markdown. No explanation outside JSON.

CURRENT CONFIG:
{json.dumps(config, indent=2)}

CONTEXT (alert payload + logs + live probe):
{logs}

IMPORTANT INSTRUCTIONS:
1. The ALERT PAYLOAD tells you WHY monitoring fired (which metric, threshold, current value).
   Use this as your PRIMARY signal for diagnosis.
2. CLOUD RUN LOGS show recent errors/warnings from the application.
3. LIVE SERVICE PROBE shows the current HTTP status when hitting the service URL.
4. Combine ALL signals to determine the root cause.

Detection rules (check alert payload fields like alertname, metric, value, labels):
- Alert mentions "error_rate" OR "5xx" OR "errors" OR logs show HTTP 500  -> issue_type: crash
- Alert mentions "latency" OR "response_time" OR "p95" OR "p99"          -> issue_type: cold_start (if no errors) or timeout (if deadline exceeded in logs)
- "Memory limit" OR "OOM" OR "out of memory" in logs                     -> issue_type: oom
- "Deadline exceeded" OR "timeout" OR "upstream timeout" in logs          -> issue_type: timeout
- "CPU throttling" in logs or alert                                       -> issue_type: high_cpu
- "No healthy upstream" OR container crash in logs                        -> issue_type: crash
- Python Traceback / Exception / SyntaxError / ImportError in logs        -> issue_type: code_error
- Live probe returns non-200 status (e.g. 500, 502, 503)                 -> issue_type: crash
- Live probe shows the service is unreachable                             -> issue_type: crash
- If alert fired but service is healthy (200) with no errors              -> Still diagnose based on the alert metric (it may be intermittent)

Fix rules (fix_type="infra" uses Terraform; fix_type="code" pushes to Bitbucket):
- oom         -> fix_type: infra, double memory (256Mi->512Mi, 512Mi->1Gi, 1Gi->2Gi)
- timeout     -> fix_type: infra, double timeout value (30->60, max 3600)
- high_cpu    -> fix_type: infra, increase cpu: "1"->"2"
- cold_start  -> fix_type: infra, min_instances: "1"
- crash       -> fix_type: infra, memory double + min_instances: "1"
- code_error  -> fix_type: code,  fix_field: "code", fix_new_value: "patched via Bitbucket"
- unknown     -> fix_type: infra, min_instances: "1"

NEVER return confidence below 0.5 if the alert payload clearly states what metric triggered.
Write a SPECIFIC root_cause that references the actual metric/threshold from the alert.

Return exactly this JSON:
{{
  "issue_type": "oom|timeout|high_cpu|crash|cold_start|code_error|unknown",
  "root_cause": "one sentence referencing the specific alert metric and what was observed",
  "fix_type": "infra|code",
  "fix_field": "memory|timeout|cpu|min_instances|max_instances|code",
  "fix_old_value": "current value from config, or 'see logs' for code_error",
  "fix_new_value": "recommended value as string, or 'patched via Bitbucket' for code_error",
  "fix_reason": "one sentence why this fixes it",
  "confidence": 0.0,
  "severity": "low|medium|high"
}}"""
    response = _model.generate_content(prompt)
    return json.loads(_strip_fences(response.text))


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
