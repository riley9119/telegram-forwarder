import os
from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, functions, utils
from telethon.sessions import StringSession
from datetime import datetime
import pytz

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")
SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")   # @username or -100id
TARGET_CHAT = os.getenv("TARGET_CHAT", "")   # @username or -100id
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional

if not all([API_ID, API_HASH, STRING_SESSION, SOURCE_CHAT, TARGET_CHAT]):
    raise RuntimeError("Missing required env vars")

app = FastAPI()
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

async def ensure_connected():
    if not client.is_connected():
        await client.connect()

def current_slot_idx() -> int:
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    return (now_local.hour // 2) % 12  # 12 posts/day => every 2 hours

async def copy_message(message_id: int):
    await ensure_connected()
    # Use the correct Telethon raw function name
    try:
        req = functions.messages.CopyMessages(
            from_peer=SOURCE_CHAT,
            id=[message_id],
            to_peer=TARGET_CHAT,
            random_id=[utils.generate_random_long()]
        )
        return await client(req)
    except AttributeError:
        # Very old Telethon version fallback: do a regular forward
        # (this will show "Forwarded from")
        return await client.forward_messages(
            entity=TARGET_CHAT,
            messages=[message_id],
            from_peer=SOURCE_CHAT
        )

@app.get("/health")
async def health():
    return {"status":"ok"}

@app.post("/hook")
async def hook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Modes:
    # 1) {"message_id": 12345}  (direct)
    # 2) {"slot":"auto"}        (use ROTATION_12_IDS and local time slot)
    if isinstance(payload, dict) and "message_id" in payload:
        message_id = int(payload["message_id"])
    elif isinstance(payload, dict) and payload.get("slot") == "auto":
        if len(ROTATION_IDS) != 12:
            raise HTTPException(status_code=400, detail="ROTATION_12_IDS must contain exactly 12 IDs")
        message_id = int(ROTATION_IDS[current_slot_idx()])
    else:
        raise HTTPException(status_code=400, detail='Provide {"message_id":N} or {"slot":"auto"}')

    try:
        res = await copy_message(message_id)
        return {"ok": True, "copied_message_id": message_id, "result": str(res)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"copyMessage failed: {e}")

