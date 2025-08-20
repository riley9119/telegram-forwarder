import os
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, functions, utils
from telethon.sessions import StringSession
from datetime import datetime
import pytz

# ---------- ENV ----------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")

# Accept @username or numeric -100xxxxxxxxxxxx. (Avoid t.me links.)
SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")
TARGET_CHAT = os.getenv("TARGET_CHAT", "")

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional shared secret

def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None

# Optional default topic (top message id) for TARGET_CHAT forum
TARGET_TOPIC_ID_ENV = os.getenv("TARGET_TOPIC_ID")
TOPIC_TOP_MSG_ID: Optional[int] = _as_int(TARGET_TOPIC_ID_ENV)

if not all([API_ID, API_HASH, STRING_SESSION, SOURCE_CHAT, TARGET_CHAT]):
    raise RuntimeError("Missing required env vars: API_ID/API_HASH/TELETHON_STRING_SESSION/SOURCE_CHAT/TARGET_CHAT")

# ---------- APP / CLIENT ----------
app = FastAPI()
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

async def ensure_connected():
    if not client.is_connected():
        await client.connect()

def current_slot_idx() -> int:
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    # 12 posts/day => every 2 hours
    return (now_local.hour // 2) % 12

async def resolve_peer(peer: Union[str, int]):
    """Return an InputPeer usable by raw TL functions."""
    await ensure_connected()
    return await client.get_input_entity(peer)

async def copy_message(
    message_id: int,
    to_peer: Optional[Union[str, int]] = None,
    topic_id: Optional[int] = None
):
    """
    Send one message from SOURCE_CHAT to TARGET_CHAT.

    If a topic_id (top_msg_id) is provided (or set via env),
    we use ForwardMessages with top_msg_id (most reliable for forum topics).
    If no topic is provided, we use CopyMessages (a true copy).
    """
    await ensure_connected()

    dest = to_peer or TARGET_CHAT
    from_peer = await resolve_peer(SOURCE_CHAT)
    to_peer_in = await resolve_peer(dest)

    top_id = _as_int(topic_id) if topic_id is not None else TOPIC_TOP_MSG_ID

    # Use ForwardMessages when targeting a Topic
    if top_id is not None:
        req = functions.messages.ForwardMessages(
            from_peer=from_peer,
            id=[int(message_id)],
            to_peer=to_peer_in,
            random_id=[utils.generate_random_long()],
            top_msg_id=int(top_id)
        )
        return await client(req)

    # No topic => do a true copy
    req = functions.messages.CopyMessages(
        from_peer=from_peer,
        id=[int(message_id)],
        to_peer=to_peer_in,
        random_id=[utils.generate_random_long()]
    )
    return await client(req)

# ---------- ROUTES ----------
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
    # 2) {"slot":"auto"} -> uses ROTATION_12_IDS and local time (2-hour slots)
    if isinstance(payload, dict) and "message_id" in payload:
        message_id = int(payload["message_id"])
    elif isinstance(payload, dict) and payload.get("slot") == "auto":
        if len(ROTATION_IDS) != 12:
            raise HTTPException(status_code=400, detail="ROTATION_12_IDS must contain exactly 12 IDs")
        message_id = int(ROTATION_IDS[current_slot_idx()])
    else:
        raise HTTPException(status_code=400, detail='Provide {"message_id":N} or {"slot":"auto"}')

    # Optional: per-request topic override
    topic_override: Optional[int] = None
    if isinstance(payload, dict) and "topic_id" in payload:
        topic_override = _as_int(payload["topic_id"])
        if payload["topic_id"] is not None and topic_override is None:
            raise HTTPException(status_code=400, detail="topic_id must be an integer")

    try:
        res = await copy_message(message_id, topic_id=topic_override)
        return {
            "ok": True,
            "copied_message_id": message_id,
            "topic_id": topic_override if topic_override is not None else TOPIC_TOP_MSG_ID,
            "result": str(res),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"copyMessage failed: {e}")
