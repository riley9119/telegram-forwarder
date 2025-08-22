import os
import logging
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.tl import functions
from datetime import datetime
import pytz

log = logging.getLogger(__name__)

# --- ENV ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")
SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")   # @username or -100id
TARGET_CHAT = os.getenv("TARGET_CHAT", "")   # @username or -100id
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Topic support (optional)
def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None

TARGET_TOPIC_ID = _as_int(os.getenv("TARGET_TOPIC_ID"))

if not all([API_ID, API_HASH, STRING_SESSION, SOURCE_CHAT, TARGET_CHAT]):
    raise RuntimeError("Missing required env vars")

# --- APP / CLIENT ---
app = FastAPI()
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

async def ensure_connected():
    if not client.is_connected():
        await client.connect()

def current_slot_idx() -> int:
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    return (now_local.hour // 2) % 12  # 12 posts/day => every 2 hours

async def copy_message(
    message_id: int,
    to_peer: Optional[Union[str, int]] = None,
    topic_id: Optional[int] = None
):
    """
    Forward a single message by ID from SOURCE_CHAT to TARGET_CHAT.
    If topic_id (top message id of a forum topic) is provided (or set via env),
    the message will be posted into that topic.
    """
    await ensure_connected()
    dest = to_peer or TARGET_CHAT
    top_id = _as_int(topic_id) or TARGET_TOPIC_ID

    # Prefer the raw ForwardMessages so we can pass top_msg_id
    kwargs = {}
    if top_id:
        kwargs["top_msg_id"] = int(top_id)

    try:
        req = functions.messages.ForwardMessages(
            from_peer=SOURCE_CHAT,
            id=[int(message_id)],
            random_id=[utils.generate_random_long()],
            to_peer=dest,
            **kwargs,   # contains top_msg_id only when we have one
        )
        return await client(req)
    except (TypeError, AttributeError) as e:
        # Telethon too old to accept top_msg_id; do a normal forward (no topic)
        log.error("ForwardMessages with top_msg_id failed; falling back to plain forward: %s", e)
        return await client.forward_messages(
            entity=dest,
            messages=[int(message_id)],
            from_peer=SOURCE_CHAT
        )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/hook")
async def hook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Modes:
    # 1) {"message_id": 12345}
    # 2) {"slot":"auto"}  -> uses ROTATION_12_IDS and local time slot
    if isinstance(payload, dict) and "message_id" in payload:
        message_id = int(payload["message_id"])
    elif isinstance(payload, dict) and payload.get("slot") == "auto":
        if len(ROTATION_IDS) != 12:
            raise HTTPException(status_code=400, detail="ROTATION_12_IDS must contain exactly 12 IDs")
        message_id = int(ROTATION_IDS[current_slot_idx()])
    else:
        raise HTTPException(status_code=400, detail='Provide {"message_id":N} or {"slot":"auto"}')

    # Optional per-request topic override
    topic_override = None
    if isinstance(payload, dict) and "topic_id" in payload:
        topic_override = _as_int(payload["topic_id"])
        if payload["topic_id"] is not None and topic_override is None:
            raise HTTPException(status_code=400, detail="topic_id must be an integer")

    try:
        res = await copy_message(message_id, topic_id=topic_override)
        return {
            "ok": True,
            "copied_message_id": message_id,
            "topic_id": topic_override if topic_override is not None else TARGET_TOPIC_ID,
            "result": str(res),
        }
    except Exception as e:
        log.exception("Hook failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
