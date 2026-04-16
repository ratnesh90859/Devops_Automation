import flow
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from config import settings
from logger import fetch_all_loki_logs
from ai import correlate_signals
from cloudrun import get_config

bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
tg_app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()

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


async def send_alert(incident: dict) -> int:
    icon = ICONS.get(incident["issue_type"], "⚠️")
    fix_type = incident.get("fix_type", "infra")
    fix_label = "Bitbucket code push" if fix_type == "code" else "Terraform infra change"

    root_cause = _esc(str(incident.get('root_cause', 'N/A')))
    fix_reason = _esc(str(incident.get('fix_reason', 'N/A')))
    fix_field = _esc(str(incident.get('fix_field', '')))
    fix_old = _esc(str(incident.get('fix_old_value', '')))
    fix_new = _esc(str(incident.get('fix_new_value', '')))

    text = (
        f"{icon} *Infra Issue Detected*\n\n"
        f"*Issue:* {incident['issue_type'].upper()}\n"
        f"*Severity:* {incident.get('severity', 'unknown').upper()}\n"
        f"*Confidence:* {int(float(incident.get('confidence', 0)) * 100)}%\n\n"
        f"*Root Cause:*\n{root_cause}\n\n"
        f"*Proposed Fix ({fix_label}):*\n"
        f"{fix_field}: {fix_old} -> {fix_new}\n"
        f"{fix_reason}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Apply Fix",
            callback_data=f"approve:{incident['id']}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject:{incident['id']}"
        ),
    ]])
    msg = await bot.send_message(
        chat_id=settings.TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return msg.message_id


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # callback may have expired — continue anyway
    action, incident_id = query.data.split(":", 1)

    if action == "approve":
        try:
            await query.edit_message_text("⏳ Applying fix... this may take a few minutes.")
        except Exception:
            pass

        try:
            result = await flow.execute_fix(incident_id)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            err_msg = _esc(str(exc)[:300])
            await bot.send_message(
                chat_id=settings.TELEGRAM_CHAT_ID,
                text=f"❌ *Fix failed with error:*\n{err_msg}",
                parse_mode="Markdown",
            )
            return

        try:
            if result["healthy"]:
                if result.get("fix_type") == "code":
                    msg_text = _esc(result.get('commit_message', ''))
                    await bot.send_message(
                        chat_id=settings.TELEGRAM_CHAT_ID,
                        text=(
                            f"✅ *Code fix applied. Service is healthy.*\n\n"
                            f"Pushed to Bitbucket and deployed via pipeline.\n"
                            f"{msg_text}"
                        ),
                        parse_mode="Markdown",
                    )
                else:
                    field = _esc(str(result.get('fix_field', '')))
                    value = _esc(str(result.get('fix_new_value', '')))
                    await bot.send_message(
                        chat_id=settings.TELEGRAM_CHAT_ID,
                        text=(
                            f"✅ *Infra fix applied. Service is healthy.*\n\n"
                            f"{field} updated to {value}"
                        ),
                        parse_mode="Markdown",
                    )

                # Fetch real incident from DB and send post-mortem
                from db import get_incident
                full_incident = get_incident(incident_id) or {}
                await send_resolution_report(full_incident, result)
            else:
                reason = result.get("error") or result.get("rolled_back_to") or "unknown"
                reason = _esc(str(reason)[:300])
                await bot.send_message(
                    chat_id=settings.TELEGRAM_CHAT_ID,
                    text=(
                        f"🔄 *Fix failed. Rolled back.*\n\n"
                        f"Reason: {reason}"
                    ),
                    parse_mode="Markdown",
                )
        except Exception as exc:
            print(f"[ERROR] sending result message: {exc}")

    elif action == "reject":
        await query.edit_message_text("❌ Rejected. No changes made.")
        try:
            await flow.reject(incident_id)
        except Exception as exc:
            print(f"[ERROR] rejecting incident: {exc}")


tg_app.add_handler(CallbackQueryHandler(handle_callback))


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
        f"  {i+1}\\. {_e(t)}" for i, t in enumerate(report.get("timeline") or [])
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
        f"  🔧 Long\\-term: {_e(report.get('longterm_fix'))}\n\n"
        f"*Prevention Strategy:*\n{prevention_lines}\n\n"
        f"*Confidence:* {conf_icon} {confidence} — {_e(report.get('confidence_reason'))}"
    )

    try:
        await bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
        )
    except Exception as exc:
        print(f"[ERROR] send_deep_report: {exc}")


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
            f"  {i+1}\\. {_esc(str(s))}" for i, s in enumerate(report.get("causal_chain") or [])
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
            f"✅ *Incident Resolved — Post\\-Mortem Report*\n\n"
            f"{layer_icon} *Root Layer:* {_esc(root_layer)}\n"
            f"*Infra Issue:* {_esc(report.get('infra_issue', 'none'))}\n"
            f"*App Issue:* {_esc(report.get('app_issue', 'none'))}\n\n"
            f"*True Root Cause:*\n{_esc(str(report.get('root_cause', 'N/A')))}\n\n"
            f"*Causal Chain:*\n{chain_lines}\n\n"
            f"*Evidence Per Layer:*\n"
            f"🏗️ Infra:\n{infra_ev if infra_ev else '  N/A'}\n"
            f"🐛 App:\n{app_ev if app_ev else '  N/A'}\n"
            f"💼 Business:\n{biz_ev if biz_ev else '  N/A'}\n\n"
            f"*Business Impact:*\n{_esc(str(report.get('business_impact', 'N/A')))}\n\n"
            f"*Correlation Insight:*\n{_esc(str(report.get('correlation_insight', 'N/A')))}\n\n"
            f"*Fix Applied:* `{fix_summary}`\n\n"
            f"*Prevention:*\n{prevent}\n\n"
            f"{conf_icon} *Confidence:* {conf} — {_esc(str(report.get('confidence_reason', '')))}"
        )

        await bot.send_message(
            chat_id=settings.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="MarkdownV2",
        )
    except Exception as exc:
        print(f"[ERROR] send_resolution_report: {exc}")
        # Send a simplified fallback
        try:
            await bot.send_message(
                chat_id=settings.TELEGRAM_CHAT_ID,
                text=(
                    f"✅ *Incident Resolved*\n\n"
                    f"Issue: {_esc(str(incident.get('issue_type', 'unknown')))}\n"
                    f"Root Cause: {_esc(str(incident.get('root_cause', 'N/A')))}\n"
                    f"Fix: {_esc(str(incident.get('fix_field', '')))}: "
                    f"{_esc(str(incident.get('fix_old_value', '')))} → "
                    f"{_esc(str(incident.get('fix_new_value', '')))}"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def setup():
    await tg_app.initialize()
    await bot.set_webhook(f"{settings.BASE_URL}/telegram")
