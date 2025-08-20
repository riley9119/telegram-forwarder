import os
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, functions, utils
from telethon.sessions import StringSession
from datetime import datetime
import pytz

# --- ENV ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")
SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")   # @username, -100id, or private invite link
TARGET_CHAT = os.getenv("TARGET_CHAT", "")   # @username or -100id
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional

def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None

TARGET_TOPIC_ID = os.getenv("TARGET_TOPIC_ID")       # e.g. "380252"
TOPIC_TOP_MSG_ID: Optional[int] = _as_int(TARGET_TOPIC_ID)

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

async def send_to_topic_via_forward(
    message_id: int,
    to_peer: Union[str, int],
    topic_id: int
):
    """Post to a forum topic using ForwardMessages (supports top_msg_id)."""
    req = functions.messages.ForwardMessages(
        from_peer=SOURCE_CHAT,
        id=[int(message_id)],
        to_peer=to_peer,
        random_id=[utils.generate_random_long()],
        top_msg_id=int(topic_id),
    )
    return await client(req)

async def copy_or_forward(
    message_id: int,
    to_peer: Optional[Union[str, int]] = None,
    topic_id: Optional[int] = None
):
    """
    - If a topic is requested: use ForwardMessages with top_msg_id (reliable for topics).
    - If no topic: try CopyMessages (clean copy). If that fails, fallback to forward.
    """
    await ensure_connected()
    dest = to_peer or TARGET_CHAT

    # If we have a topic to use (payload override > env), forward into topic
    effective_topic = topic_id if topic_id is not None else TOPIC_TOP_MSG_ID
    if effective_topic is not None:
        return await send_to_topic_via_forward(message_id, dest, effective_topic)

    # No topic -> prefer CopyMessages (no "Forwarded from")
    try:
        req = functions.messages.CopyMessages(
            from_peer=SOURCE_CHAT,
            id=[int(message_id)],
            to_peer=dest,
            random_id=[utils.generate_random_long()],
        )
        return await client(req)
    except Exception:
        # Fallback to a regular forward if copy is unavailable
        req2 = functions.messages.ForwardMessages(
            from_peer=SOURCE_CHAT,
            id=[int(message_id)],
            to_peer=dest,
            random_id=[utils.generate_random_long()],
        )
        return await client(req2)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/hook")
async def hook(request: Request):
    # Optional shared secret
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
        res = await copy_or_forward(message_id, topic_id=topic_override)
        return {
            "ok": True,
            "copied_message_id": message_id,
            "topic_id": topic_override if topic_override is not None else TOPIC_TOP_MSG_ID,
            "result": str(res),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"send failed: {e}")
