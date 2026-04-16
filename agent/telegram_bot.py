import flow
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from config import settings

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


async def setup():
    await tg_app.initialize()
    await bot.set_webhook(f"{settings.BASE_URL}/telegram")
