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


async def send_alert(incident: dict) -> int:
    icon = ICONS.get(incident["issue_type"], "⚠️")
    fix_type = incident.get("fix_type", "infra")
    fix_label = "🔧 Bitbucket code push" if fix_type == "code" else "⚙️ Terraform infra change"

    text = (
        f"{icon} *Infra Issue Detected*\n\n"
        f"*Issue:* {incident['issue_type'].upper()}\n"
        f"*Severity:* {incident['severity'].upper()}\n"
        f"*Confidence:* {int(incident['confidence'] * 100)}%\n\n"
        f"*Root Cause:*\n{incident['root_cause']}\n\n"
        f"*Proposed Fix ({fix_label}):*\n"
        f"Change `{incident['fix_field']}` "
        f"from `{incident['fix_old_value']}` "
        f"\u2192 `{incident['fix_new_value']}`\n"
        f"_{incident['fix_reason']}_"
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
    await query.answer()
    action, incident_id = query.data.split(":", 1)

    if action == "approve":
        await query.edit_message_text("⏳ Applying fix... this may take a few minutes.")

        try:
            result = await flow.execute_fix(incident_id)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            await bot.send_message(
                chat_id=settings.TELEGRAM_CHAT_ID,
                text=f"❌ *Fix failed with error:*\n`{str(exc)[:500]}`",
                parse_mode="Markdown",
            )
            return

        try:
            if result["healthy"]:
                if result.get("fix_type") == "code":
                    await bot.send_message(
                        chat_id=settings.TELEGRAM_CHAT_ID,
                        text=(
                            f"✅ *Code fix applied. Service is healthy.*\n\n"
                            f"📦 Pushed to Bitbucket & deployed via pipeline.\n"
                            f"_{result.get('commit_message', '')}_ \n"
                            f"Pipeline: `{result.get('pipeline_uuid', 'n/a')}`"
                        ),
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        chat_id=settings.TELEGRAM_CHAT_ID,
                        text=(
                            f"✅ *Infra fix applied. Service is healthy.*\n\n"
                            f"`{result.get('fix_field', '')}` \u2192 `{result.get('fix_new_value', '')}`"
                        ),
                        parse_mode="Markdown",
                    )
            else:
                reason = result.get("error") or result.get("rolled_back_to") or "unknown"
                await bot.send_message(
                    chat_id=settings.TELEGRAM_CHAT_ID,
                    text=(
                        f"🔄 *Fix failed. Rolled back.*\n\n"
                        f"Reason: `{reason}`"
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


async def setup():
    await tg_app.initialize()
    await bot.set_webhook(f"{settings.BASE_URL}/telegram")
