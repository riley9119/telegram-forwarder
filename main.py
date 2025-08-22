import os
import logging
import traceback
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, utils, functions
from telethon.sessions import StringSession
from telethon.tl.types import InputReplyToMessage
from datetime import datetime
import pytz

# ---------- ENV ----------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")

SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")          # @username or -100id or t.me link
TARGET_CHAT = os.getenv("TARGET_CHAT", "")          # @username or -100id
TARGET_TOPIC_ID = os.getenv("TARGET_TOPIC_ID", "")  # e.g. "380252"

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

if not all([API_ID, API_HASH, STRING_SESSION, SOURCE_CHAT, TARGET_CHAT]):
    raise RuntimeError("Missing required env vars: API_ID/API_HASH/STRING_SESSION/SOURCE_CHAT/TARGET_CHAT")

# ---------- APP / CLIENT ----------
app = FastAPI()
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

logging.basicConfig(level=logging.INFO)

async def ensure_connected():
    if not client.is_connected():
        await client.connect()

def _as_int(v: Optional[Union[str, int]]) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None

DEFAULT_TOPIC_ID = _as_int(TARGET_TOPIC_ID)

def current_slot_idx() -> int:
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    # 12 posts/day → every 2 hours
    return (now_local.hour // 2) % 12

async def copy_message(message_id: int,
                       to_peer: Optional[Union[str, int]] = None,
                       topic_id: Optional[int] = None):
    """
    Copy/forward a single message from SOURCE_CHAT to TARGET_CHAT.
    If topic_id is provided (or via env), post inside that forum topic.
    """
    await ensure_connected()

    # Resolve peers to avoid username / id ambiguity
    src = await client.get_input_entity(SOURCE_CHAT)
    dest = await client.get_input_entity(to_peer or TARGET_CHAT)

    # Build reply_to for topic posting (both fields must be set)
    reply_to = None
    topic_id_int = _as_int(topic_id if topic_id is not None else DEFAULT_TOPIC_ID)
    if topic_id_int:
        reply_to = InputReplyToMessage(
            reply_to_msg_id=topic_id_int,
            top_msg_id=topic_id_int
        )

    # Prefer CopyMessages if available in this Telethon version
    CopyReq = getattr(functions.messages, "CopyMessages", None)

    try:
        if CopyReq is not None:
            req = CopyReq(
                from_peer=src,
                id=[int(message_id)],
                to_peer=dest,
                random_id=[utils.generate_random_long()],
                reply_to=reply_to  # for topics
            )
            return await client(req)
        else:
            # Fallback: forward (will show “Forwarded from”).
            # Use top_msg_id so it goes into the topic thread.
            return await client.forward_messages(
                entity=dest,
                messages=int(message_id),
                from_peer=src,
                top_msg_id=topic_id_int if topic_id_int else None
            )
    except Exception as e:
        logging.exception("copy_message failed")
        raise

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/hook")
async def hook(request: Request):
    # optional shared secret
    if WEBHOOK_SECRET and request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Modes:
    # 1) {"message_id": 12345}
    # 2) {"slot": "auto"}  -> pick from ROTATION_12_IDS according to 2-hour slot
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
            "topic_id": topic_override if topic_override is not None else DEFAULT_TOPIC_ID,
            "result": str(res)
        }
    except Exception as e:
        # Surface the real reason to logs + response
        logging.error("Hook failed: %s", e)
        logging.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"copyMessage failed: {e}")
