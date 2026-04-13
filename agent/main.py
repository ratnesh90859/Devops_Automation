from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from telegram import Update
from telegram_bot import send_alert, setup as tg_setup, tg_app
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
        incident = await handle_alert(source, service_url)
        await send_alert(incident)

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
