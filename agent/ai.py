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
    prompt = f"""You are a GCP Cloud Run expert.
Read the logs and current config. Return a JSON fix.
Raw JSON only. No markdown. No explanation outside JSON.

CURRENT CONFIG:
{json.dumps(config, indent=2)}

LOGS:
{logs}

Detection rules:
- "Memory limit" OR "OOM" OR "out of memory"                -> issue_type: oom
- "Deadline exceeded" OR "timeout" OR "upstream timeout"    -> issue_type: timeout
- "CPU throttling"                                          -> issue_type: high_cpu
- "No healthy upstream" OR "crash"                          -> issue_type: crash
- p95 latency consistently high + no errors                 -> issue_type: cold_start
- Python Traceback / Exception / SyntaxError / ImportError  -> issue_type: code_error

Fix rules (fix_type="infra" uses Terraform; fix_type="code" pushes to Bitbucket):
- oom         -> fix_type: infra, double memory (256Mi->512Mi, 512Mi->1Gi, 1Gi->2Gi)
- timeout     -> fix_type: infra, double timeout value (30->60, max 3600)
- high_cpu    -> fix_type: infra, increase cpu: "1"->"2"
- cold_start  -> fix_type: infra, min_instances: "1"
- crash       -> fix_type: infra, memory double + min_instances: "1"
- code_error  -> fix_type: code,  fix_field: "code", fix_new_value: "patched via Bitbucket"
- unknown     -> fix_type: infra, min_instances: "1"

Return exactly this JSON:
{{
  "issue_type": "oom|timeout|high_cpu|crash|cold_start|code_error|unknown",
  "root_cause": "one sentence max",
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
