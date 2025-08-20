import os
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, functions, utils, types
from telethon.sessions import StringSession
from datetime import datetime
import pytz

# --- ENV ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")

SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")   # @username or -100id or t.me/...
TARGET_CHAT = os.getenv("TARGET_CHAT", "")   # @username or -100id
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")

ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional shared secret

def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None

# Forum topic support (numeric message ID of the topic starter message)
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
    # 12 posts/day => every 2 hours
    return (now_local.hour // 2) % 12


async def _re_send_to_topic(
    src_msg_id: int,
    dest_peer: Union[str, int],
    topic_top_msg_id: int
):
    """
    Re-send a source message (text/media/buttons) into a specific forum topic, by
    replying to the topic's top message (top_msg_id).
    This avoids the 'Forwarded from' label and works for forum topics.
    """
    await ensure_connected()

    # fetch source message
    msg = await client.get_messages(SOURCE_CHAT, ids=src_msg_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Source message not found")

    reply_to = types.InputReplyToMessage(top_msg_id=int(topic_top_msg_id))

    # Re-send media or text
    if msg.media:
        # send_file accepts message.media directly; caption is msg.message (may be None)
        return await client.send_file(
            dest_peer,
            file=msg.media,
            caption=msg.message or "",
            reply_to=reply_to,
            buttons=msg.buttons
        )
    else:
        # plain text (keep buttons if present)
        return await client.send_message(
            dest_peer,
            msg.message or "",
            reply_to=reply_to,
            buttons=msg.buttons
        )


async def copy_message(
    message_id: int,
    to_peer: Optional[Union[str, int]] = None,
    topic_id: Optional[int] = None
):
    """
    Copy a single message by ID from SOURCE_CHAT to TARGET_CHAT.
    - If a topic_id (or TARGET_TOPIC_ID) is provided, re-send the message into that topic.
    - Otherwise, use MTProto copy (no 'Forwarded from' label).
    """
    await ensure_connected()
    dest = to_peer or TARGET_CHAT

    # If we have a topic to target, re-send (copy) into that topic.
    effective_topic = topic_id if topic_id is not None else TARGET_TOPIC_ID
    if effective_topic is not None:
        return await _re_send_to_topic(message_id, dest, effective_topic)

    # No topic -> use raw copy (keeps caption/media without 'Forwarded from')
    try:
        req = functions.messages.CopyMessages(
            from_peer=SOURCE_CHAT,
            id=[int(message_id)],
            to_peer=dest,
            random_id=[utils.generate_random_long()],
            # No top_msg_id here (CopyMessages does not support it)
        )
        return await client(req)
    except Exception:
        # Fallback: forward (will show "Forwarded from")
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
        res = await copy_message(message_id, topic_id=topic_override)
        # Try to return some useful identifiers
        copied_id = None
        try:
            # Telethon Message object (send_message/send_file path)
            copied_id = getattr(res, "id", None)
        except Exception:
            pass

        return {
            "ok": True,
            "copied_message_id": message_id,
            "new_message_id": copied_id,
            "topic_id": topic_override if topic_override is not None else TARGET_TOPIC_ID,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"copyMessage failed: {e}")
