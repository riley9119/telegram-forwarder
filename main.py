import os
import logging
from typing import Optional, Union

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, utils, __version__ as telethon_version
from telethon.sessions import StringSession
from datetime import datetime
import pytz

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("forwarder")

# Try to import raw requests under whichever name exists in this Telethon
CopyMessages = ForwardMessages = None
try:
    from telethon.tl.functions.messages import CopyMessages as _CopyMessages
    CopyMessages = _CopyMessages
except Exception:
    try:
        from telethon.tl.functions.messages import CopyMessagesRequest as _CopyMessagesReq
        CopyMessages = _CopyMessagesReq
    except Exception:
        CopyMessages = None

try:
    from telethon.tl.functions.messages import ForwardMessages as _ForwardMessages
    ForwardMessages = _ForwardMessages
except Exception:
    try:
        from telethon.tl.functions.messages import ForwardMessagesRequest as _ForwardMessagesReq
        ForwardMessages = _ForwardMessagesReq
    except Exception:
        ForwardMessages = None

# ---------- env ----------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("TELETHON_STRING_SESSION", "")
SOURCE_CHAT = os.getenv("SOURCE_CHAT", "")   # @username or -100id
TARGET_CHAT = os.getenv("TARGET_CHAT", "")   # @username or -100id
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TARGET_TOPIC_ID_ENV = os.getenv("TARGET_TOPIC_ID", "")

def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None

TOPIC_TOP_MSG_ID = _as_int(TARGET_TOPIC_ID_ENV)

if not all([API_ID, API_HASH, STRING_SESSION, SOURCE_CHAT, TARGET_CHAT]):
    raise RuntimeError("Missing required env vars")

# ---------- app/client ----------
app = FastAPI()
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

@app.on_event("startup")
async def _startup():
    log.info("Telethon version: %s", telethon_version)
    log.info(
        "Raw requests present -> CopyMessages: %s | ForwardMessages: %s",
        bool(CopyMessages), bool(ForwardMessages)
    )

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
    Copy/forward a single message from SOURCE_CHAT -> TARGET_CHAT.
    Uses top_msg_id (forum topic) whenever the Telethon build exposes
    the raw requests. Falls back to plain forward (no topic) only as
    a last resort.
    """
    await ensure_connected()
    dest = to_peer or TARGET_CHAT
    top_id = _as_int(topic_id) or TOPIC_TOP_MSG_ID

    # Try CopyMessages first (keeps media/captions intact)
    if CopyMessages is not None:
        try:
            log.info("Using raw CopyMessages with top_msg_id=%s", top_id)
            req = CopyMessages(
                from_peer=SOURCE_CHAT,
                id=[int(message_id)],
                to_peer=dest,
                random_id=[utils.generate_random_long()],
                **({"top_msg_id": top_id} if top_id else {})
            )
            return await client(req)
        except Exception as e:
            log.warning("CopyMessages failed (%s); trying ForwardMessages…", e)

    # Then try raw ForwardMessages (also supports top_msg_id)
    if ForwardMessages is not None:
        try:
            log.info("Using raw ForwardMessages with top_msg_id=%s", top_id)
            req = ForwardMessages(
                from_peer=SOURCE_CHAT,
                id=[int(message_id)],
                to_peer=dest,
                random_id=[utils.generate_random_long()],
                **({"top_msg_id": top_id} if top_id else {})
            )
            return await client(req)
        except Exception as e:
            log.warning("ForwardMessages failed (%s); falling back to plain forward (no topic).", e)

    # Last resort: high-level helper (no top_msg_id support)
    log.error("No raw request available; forwarding without topic_id.")
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

    # modes
    if isinstance(payload, dict) and "message_id" in payload:
        message_id = int(payload["message_id"])
    elif isinstance(payload, dict) and payload.get("slot") == "auto":
        if len(ROTATION_IDS) != 12:
            raise HTTPException(status_code=400, detail="ROTATION_12_IDS must contain exactly 12 IDs")
        message_id = int(ROTATION_IDS[current_slot_idx()])
    else:
        raise HTTPException(status_code=400, detail='Provide {"message_id":N} or {"slot":"auto"}')

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
            "topic_id": topic_override if topic_override is not None else TOPIC_TOP_MSG_ID,
            "result": str(res),
        }
    except Exception as e:
        log.exception("Hook failed: %s", e)
        raise HTTPException(status_code=500, detail=f"copyMessage failed: {e}")
