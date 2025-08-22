import os
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, utils
from telethon.tl import functions, types
from telethon.sessions import StringSession
from datetime import datetime
import pytz
import traceback

# --- ENV ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")

SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")      # @username or -100id
TARGET_CHAT = os.getenv("TARGET_CHAT", "")      # @username or -100id

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional

# Optional default forum topic (top message id)
def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None

TARGET_TOPIC_ID = _as_int(os.getenv("TARGET_TOPIC_ID", ""))

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
    return (now_local.hour // 2) % 12  # 12 slots/day => every 2 hours

async def copy_message(
    message_id: int,
    to_peer: Optional[Union[str, int]] = None,
    topic_id: Optional[int] = None
):
    """
    Copy one message from SOURCE_CHAT to TARGET_CHAT (or to_peer).
    If topic_id (top message id of a forum topic) is provided (or configured),
    try to post into that topic using the most compatible parameter available.
    """
    await ensure_connected()

    dest = to_peer or TARGET_CHAT
    top_id = _as_int(topic_id) if topic_id is not None else TARGET_TOPIC_ID

    # Build kwargs for CopyMessages in a way that works across Telethon versions.
    # 1) Prefer the modern 'reply_to=InputReplyToMessage(top_msg_id=...)'
    # 2) If TypeError, try legacy 'top_msg_id='
    # 3) If neither works, fall back to a plain forward (no topic support).
    #    (We do this to avoid 500s.)
    try:
        if top_id is not None:
            reply_to = types.InputReplyToMessage(top_msg_id=int(top_id))
            req = functions.messages.CopyMessages(
                from_peer=SOURCE_CHAT,
                id=[int(message_id)],
                to_peer=dest,
                random_id=[utils.generate_random_long()],
                reply_to=reply_to,
            )
        else:
            req = functions.messages.CopyMessages(
                from_peer=SOURCE_CHAT,
                id=[int(message_id)],
                to_peer=dest,
                random_id=[utils.generate_random_long()],
            )
        return await client(req)

    except TypeError as e:
        # Older Telethon: try top_msg_id=...
        try:
            if top_id is not None:
                req = functions.messages.CopyMessages(
                    from_peer=SOURCE_CHAT,
                    id=[int(message_id)],
                    to_peer=dest,
                    random_id=[utils.generate_random_long()],
                    top_msg_id=int(top_id),
                )
            else:
                req = functions.messages.CopyMessages(
                    from_peer=SOURCE_CHAT,
                    id=[int(message_id)],
                    to_peer=dest,
                    random_id=[utils.generate_random_long()],
                )
            return await client(req)
        except Exception as e2:
            # Fallthrough to forward below
            print("CopyMessages with reply_to/top_msg_id failed; falling back to forward:", e2)
            traceback.print_exc()

    except Exception as e:
        # Any other CopyMessages failure -> fall back to forward
        print("CopyMessages failed; falling back to forward:", e)
        traceback.print_exc()

    # Fallback (may not respect topics on very old Telethon)
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

    # Body options:
    # 1) {"message_id": 12345}
    # 2) {"slot": "auto"} -> picks from ROTATION_12_IDS by local time
    if isinstance(payload, dict) and "message_id" in payload:
        message_id = int(payload["message_id"])
    elif isinstance(payload, dict) and payload.get("slot") == "auto":
        if len(ROTATION_IDS) != 12:
            raise HTTPException(status_code=400, detail="ROTATION_12_IDS must contain exactly 12 IDs")
        message_id = int(ROTATION_IDS[current_slot_idx()])
    else:
        raise HTTPException(status_code=400, detail='Provide {"message_id":N} or {"slot":"auto"}')

    # Optional topic override per request
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
        # Show the exact failure text in Render logs / response
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"copyMessage failed: {e}")
