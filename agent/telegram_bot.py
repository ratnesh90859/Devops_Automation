import asyncio
import flow
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters as tg_filters
from telegram.request import HTTPXRequest
from config import settings
from logger import fetch_all_loki_logs
from ai import correlate_signals
from cloudrun import get_config

# Use a larger connection pool to avoid "Pool timeout" when multiple alerts fire
_request = HTTPXRequest(pool_timeout=30, connection_pool_size=20, read_timeout=30, write_timeout=30)
bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, request=_request)
tg_app = (
    Application.builder()
    .token(settings.TELEGRAM_BOT_TOKEN)
    .http_version("1.1")
    .get_updates_http_version("1.1")
    .pool_timeout(30)
    .connection_pool_size(20)
    .read_timeout(30)
    .write_timeout(30)
    .build()
)

# Serialise outbound Telegram messages to avoid connection pool starvation
_send_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Multi-step approval state
# Keyed by chat_id so concurrent incidents in different chats work correctly.
# ---------------------------------------------------------------------------
# Structure: {chat_id: {"incident_id": str, "rejecting": bool}}
_pending_reasons: dict = {}

ICONS = {
    "oom": "💾",
    "timeout": "⏱️",
    "high_cpu": "🔥",
    "crash": "💥",
    "cold_start": "🥶",
    "code_error": "🐛",
    "unknown": "❓",
}


def _esc(text: str) -> str:
    """Escape Telegram Markdown special characters in dynamic text."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


async def _safe_send(chat_id=None, **kwargs):
    """Send a Telegram message with lock + retry to avoid pool timeout."""
    if chat_id is None:
        chat_id = settings.TELEGRAM_CHAT_ID
    for attempt in range(3):
        try:
            async with _send_lock:
                return await bot.send_message(chat_id=chat_id, **kwargs)
        except Exception as exc:
            print(f"[WARN] Telegram send attempt {attempt+1} failed: {exc}")
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    return None


async def send_alert(incident: dict) -> int:
    icon = ICONS.get(incident["issue_type"], "⚠️")
    fix_type = incident.get("fix_type", "infra")
    fix_label = "Bitbucket code push" if fix_type == "code" else "Terraform infra change"

    root_cause = _esc(str(incident.get('root_cause') or 'N/A'))
    fix_reason = _esc(str(incident.get('fix_reason') or 'N/A'))
    fix_field  = _esc(str(incident.get('fix_field') or 'N/A'))
    fix_old    = _esc(str(incident.get('fix_old_value') or 'N/A'))
    fix_new    = _esc(str(incident.get('fix_new_value') or 'N/A'))

    issue_type_esc = _esc((incident.get('issue_type') or 'unknown').upper())
    severity_esc   = _esc((incident.get('severity') or 'unknown').upper())
    conf_pct       = int(float(incident.get('confidence') or 0) * 100)

    # Pull live metrics from the alert body if present (threshold_monitor adds these)
    alert_body   = incident.get("alert_body") or {}
    total_reqs   = alert_body.get("total_requests", "")
    total_errs   = alert_body.get("total_errors", "")
    error_rate   = alert_body.get("error_rate_pct", "")
    memory_mb    = alert_body.get("memory_mb", "")
    triggered_by = _esc(str(alert_body.get("alertname", incident.get("source", ""))))

    # Build optional metrics line
    metrics_parts = []
    if total_reqs != "":
        metrics_parts.append(f"Requests: {total_reqs}")
    if total_errs != "":
        metrics_parts.append(f"Errors: {total_errs}")
    if error_rate != "":
        metrics_parts.append(f"Error rate: {error_rate}%")
    if memory_mb != "":
        metrics_parts.append(f"Memory: {memory_mb} MB")
    metrics_line = f"*Live Metrics:* {_esc(', '.join(metrics_parts))}\n" if metrics_parts else ""

    # Determine if this is a code or infra issue for the header
    is_code_issue = (incident.get("fix_type") == "code" or
                     incident.get("issue_type") == "code_error")
    header = "🐛 *Code Bug Detected*" if is_code_issue else f"{icon} *Infra Issue Detected*"

    # Format fix display differently for code vs infra
    if is_code_issue:
        fix_display = (
            f"*Proposed Fix ({fix_label}):*\n"
            f"🔧 Auto-patch application code via Bitbucket CI/CD\n"
            f"_{fix_reason}_"
        )
    else:
        fix_display = (
            f"*Proposed Fix ({fix_label}):*\n"
            f"`{fix_field}`: {fix_old} → {fix_new}\n"
            f"_{fix_reason}_"
        )

    text = (
        f"{header}\n\n"
        f"*Issue:* {issue_type_esc}\n"
        f"*Severity:* {severity_esc}\n"
        f"*Confidence:* {conf_pct}%\n"
        f"*Triggered by:* {triggered_by}\n"
        f"{metrics_line}\n"
        f"*Root Cause:*\n{root_cause}\n\n"
        f"{fix_display}\n\n"
        f"_Type your reason after clicking a button below._"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply Fix", callback_data=f"approve:{incident['id']}"),
        InlineKeyboardButton("❌ Reject",    callback_data=f"reject:{incident['id']}"),
    ]])
    msg = await _safe_send(
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return msg.message_id if msg else 0


async def _do_create_pr(chat_id: int, incident_id: str, approver: str, reason: str) -> None:
    """
    PR-based approval path (enterprise flow):
      1. User clicked ✅ in Telegram and typed a reason
      2. We create a fix branch + commit changes + open a Bitbucket PR
      3. We send the PR link to Telegram — the user reviews it in Bitbucket
      4. When the PR is merged, the pipeline runs and applies the fix
      5. Pipeline success webhook triggers a post-mortem report

    This function REPLACES the old _do_apply_fix / direct Terraform path.
    """
    from db import get_incident
    incident = get_incident(incident_id)
    fix_type = incident.get("fix_type", "infra")

    await _safe_send(
        chat_id=chat_id,
        text=(
            f"⏳ *Creating Fix PR…*\n\n"
            f"*Approved by:* {_esc(approver)}\n"
            f"*Reason:* {_esc(reason)}\n\n"
            f"Building branch and PR in Bitbucket — should take ~10 seconds."
        ),
        parse_mode="Markdown",
    )

    try:
        result = await flow.create_fix_pr(incident)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        err_msg = _esc(str(exc)[:400])
        await _safe_send(
            chat_id=chat_id,
            text=(
                f"❌ *PR creation failed*\n\n"
                f"{err_msg}\n\n"
                f"_Check Bitbucket API token permissions (Repositories read/write, Pull Requests write)._"
            ),
            parse_mode="Markdown",
        )
        return

    pr_url    = result.get("pr_url", "")
    pr_id     = result.get("pr_id", "")
    branch    = result.get("branch", "")
    fix_label = {
        "infra":    "Terraform change (memory / cpu / timeout)",
        "code":     "Application code patch",
        "rollback": "Rollback to previous image",
    }.get(fix_type, fix_type)

    await _safe_send(
        chat_id=chat_id,
        text=(
            f"✅ *Fix PR Created — Waiting for your Bitbucket Approval*\n\n"
            f"*Approved by:* {_esc(approver)}\n"
            f"*Reason:* {_esc(reason)}\n\n"
            f"*Fix type:* {_esc(fix_label)}\n"
            f"*Branch:* `{_esc(branch)}`\n"
            f"*PR #{pr_id}:* {pr_url}\n\n"
            f"*Next steps:*\n"
            f"  1. Open the PR link above in Bitbucket\n"
            f"  2. Review the changes (Terraform tfvars diff OR code diff)\n"
            f"  3. Approve and merge the PR\n"
            f"  4. Pipeline runs automatically: terraform apply / build+deploy\n"
            f"  5. You will receive a post-mortem report here when done\n\n"
            f"_No changes have been made yet. Your merge is the gate._"
        ),
        parse_mode="Markdown",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Step 1 of the approval flow:
      • ✅ Approve  → ask the operator to type their reason
      • ❌ Reject   → ask the operator to type their reason
    Step 2 is handled by handle_text_input below.
    """
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # callback may have expired — continue anyway

    action, incident_id = query.data.split(":", 1)
    chat_id = query.message.chat_id

    if action == "approve":
        _pending_reasons[chat_id] = {"incident_id": incident_id, "rejecting": False}
        try:
            await query.edit_message_text(
                "📋 *Approval Required*\n\n"
                "Please reply with your *approval reason* to apply the fix:\n\n"
                "_e.g. 'Memory pressure confirmed in logs — safe to scale up'_",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    elif action == "reject":
        _pending_reasons[chat_id] = {"incident_id": incident_id, "rejecting": True}
        try:
            await query.edit_message_text(
                "📋 *Rejection Reason Required*\n\n"
                "Please reply with your *rejection reason* to discard this fix:\n\n"
                "_e.g. 'Will handle manually during scheduled maintenance window'_",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Step 2 of the approval flow: receive the typed reason and act on it.
    Only activates when this chat has a pending approval/rejection.
    """
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id
    if chat_id not in _pending_reasons:
        return  # This chat is not mid-approval — ignore the message

    state       = _pending_reasons.pop(chat_id)
    incident_id = state["incident_id"]
    reason      = update.message.text.strip()
    is_reject   = state.get("rejecting", False)
    user        = update.effective_user
    approver    = user.username or user.first_name or str(user.id) if user else "operator"

    if is_reject:
        await update.message.reply_text(
            f"❌ *Fix rejected*\n\n"
            f"*Rejected by:* {_esc(approver)}\n"
            f"*Reason:* {_esc(reason)}\n\n"
            f"No changes have been made.",
            parse_mode="Markdown",
        )
        try:
            await flow.reject(incident_id, reason=reason)
        except Exception as exc:
            print(f"[ERROR] rejecting incident: {exc}")
        return

    # ── Approve path ──────────────────────────────────────────────────────────
    try:
        from db import update_incident
        update_incident(incident_id, {
            "approval_reason": reason,
            "approved_by": approver,
        })
    except Exception:
        pass

    await _do_create_pr(chat_id, incident_id, approver, reason)


tg_app.add_handler(CallbackQueryHandler(handle_callback))
tg_app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_text_input))


async def send_deep_report(incident: dict) -> None:
    """Send the full structured SRE report as a second Telegram message."""
    report = incident.get("deep_report")
    if not report:
        return

    def _e(val) -> str:
        return _esc(str(val)) if val else "N/A"

    evidence_lines = "\n".join(
        f"  • {_e(e)}" for e in (report.get("key_evidence") or [])
    )
    timeline_lines = "\n".join(
        f"  {i+1}. {_e(t)}" for i, t in enumerate(report.get("timeline") or [])
    )
    prevention_lines = "\n".join(
        f"  • {_e(p)}" for p in (report.get("prevention") or [])
    )

    confidence = report.get("confidence", "?")
    conf_icon = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(confidence, "⚪")

    text = (
        f"📋 *Deep SRE Analysis Report*\n\n"
        f"*Issue Classification:* {_e(report.get('issue_classification'))}\n\n"
        f"*Root Cause:*\n{_e(report.get('root_cause'))}\n\n"
        f"*Key Evidence:*\n{evidence_lines}\n\n"
        f"*Timeline:*\n{timeline_lines}\n\n"
        f"*Business Impact:*\n{_e(report.get('business_impact'))}\n\n"
        f"*Recommended Fix:*\n"
        f"  ⚡ Immediate: {_e(report.get('immediate_fix'))}\n"
        f"  🔧 Long-term: {_e(report.get('longterm_fix'))}\n\n"
        f"*Prevention Strategy:*\n{prevention_lines}\n\n"
        f"*Confidence:* {conf_icon} {confidence} — {_e(report.get('confidence_reason'))}"
    )

    await _safe_send(text=text, parse_mode="Markdown")


async def send_resolution_report(incident: dict, fix_result: dict) -> None:
    """
    Post-incident resolution report sent to Telegram after a successful fix.
    Fetches REAL logs from GCP Cloud Logging, correlates all 3 layers,
    and shows the true root cause + business impact.
    """
    try:
        # Fetch fresh logs NOW (after the incident, to see what actually happened)
        loki_logs = fetch_all_loki_logs(minutes=30)
        config    = await get_config()

        # Build metrics summary from the incident
        metrics = {
            "issue_type":    incident.get("issue_type", "unknown"),
            "severity":      incident.get("severity", "unknown"),
            "confidence":    incident.get("confidence", 0),
            "fix_field":     incident.get("fix_field", ""),
            "fix_old_value": incident.get("fix_old_value", ""),
            "fix_new_value": incident.get("fix_new_value", ""),
        }

        report = await correlate_signals(
            infra_logs=loki_logs.get("infra", ""),
            app_logs=loki_logs.get("app", ""),
            business_logs=loki_logs.get("business", ""),
            metrics=metrics,
            config=config,
        )

        # Format causal chain
        chain_lines = "\n".join(
            f"  {i+1}. {_esc(str(s))}" for i, s in enumerate(report.get("causal_chain") or [])
        )
        infra_ev = "\n".join(f"  • {_esc(str(e))}" for e in (report.get("infra_evidence") or []))
        app_ev   = "\n".join(f"  • {_esc(str(e))}" for e in (report.get("app_evidence") or []))
        biz_ev   = "\n".join(f"  • {_esc(str(e))}" for e in (report.get("business_evidence") or []))
        prevent  = "\n".join(f"  • {_esc(str(p))}" for p in (report.get("prevention") or []))

        conf     = report.get("confidence", "?")
        conf_icon = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(conf, "⚪")

        root_layer = report.get("root_layer", "unknown").upper()
        layer_icon = {"INFRASTRUCTURE": "🏗️", "APPLICATION": "🐛", "BOTH": "⚡"}.get(root_layer, "❓")

        # Fix summary line
        if fix_result.get("fix_type") == "code":
            fix_summary = _esc(fix_result.get("commit_message", "Code patch deployed"))
        else:
            field = _esc(str(fix_result.get("fix_field", "")))
            old   = _esc(str(fix_result.get("fix_old_value", "")))
            new   = _esc(str(fix_result.get("fix_new_value", "")))
            fix_summary = f"{field}: {old} → {new}"

        text = (
            f"✅ *Incident Resolved - Post-Mortem Report*\n\n"
            f"{layer_icon} *Root Layer:* {_esc(root_layer)}\n"
            f"*Infra Issue:* {_esc(report.get('infra_issue', 'none'))}\n"
            f"*App Issue:* {_esc(report.get('app_issue', 'none'))}\n\n"
            f"*True Root Cause:*\n{_esc(str(report.get('root_cause') or 'N/A'))}\n\n"
            f"*Causal Chain:*\n{chain_lines}\n\n"
            f"*Evidence Per Layer:*\n"
            f"🏗️ Infra:\n{infra_ev if infra_ev else '  N/A'}\n"
            f"🐛 App:\n{app_ev if app_ev else '  N/A'}\n"
            f"💼 Business:\n{biz_ev if biz_ev else '  N/A'}\n\n"
            f"*Business Impact:*\n{_esc(str(report.get('business_impact') or 'N/A'))}\n\n"
            f"*Correlation Insight:*\n{_esc(str(report.get('correlation_insight') or 'N/A'))}\n\n"
            f"*Fix Applied:* {fix_summary}\n\n"
            f"*Prevention:*\n{prevent}\n\n"
            f"{conf_icon} *Confidence:* {conf} — {_esc(str(report.get('confidence_reason', '')))}"
        )

        await _safe_send(text=text, parse_mode="Markdown")
    except Exception as exc:
        print(f"[ERROR] send_resolution_report: {exc}")
        await _safe_send(
            text=(
                f"✅ *Incident Resolved*\n\n"
                f"Issue: {_esc(str(incident.get('issue_type') or 'unknown'))}\n"
                f"Root Cause: {_esc(str(incident.get('root_cause') or 'N/A'))}\n"
                f"Fix: {_esc(str(incident.get('fix_field') or ''))}: "
                f"{_esc(str(incident.get('fix_old_value') or ''))} → "
                f"{_esc(str(incident.get('fix_new_value') or ''))}"
            ),
            parse_mode="Markdown",
        )


async def setup():
    await tg_app.initialize()
    await bot.set_webhook(f"{settings.BASE_URL}/telegram")
