import os
import logging
from typing import Optional, Union, List, Tuple

from fastapi import FastAPI, Request, HTTPException
from telethon import TelegramClient, utils, __version__ as telethon_version
from telethon.sessions import StringSession
from datetime import datetime
import pytz

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("forwarder")

# Try to import raw requests under whichever name exists
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

TARGET_CHAT = os.getenv("TARGET_CHAT", "")   # legacy single-target default
TARGET_TOPIC_ID_ENV = os.getenv("TARGET_TOPIC_ID", "")  # legacy default topic
TARGETS_ENV = os.getenv("TARGETS", "")       # NEW: multi-target list: chat[:topic_id],chat[:topic_id]

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
ROTATION_IDS = [s.strip() for s in os.getenv("ROTATION_12_IDS", "").split(",") if s.strip()]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

def _as_int(v) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None

DEFAULT_TOPIC_ID = _as_int(TARGET_TOPIC_ID_ENV)

if not all([API_ID, API_HASH, STRING_SESSION, SOURCE_CHAT]):
    raise RuntimeError("Missing required env vars")

# Parse TARGETS env -> list of (chat, topic_id)
def _parse_targets(s: str) -> List[Tuple[str, Optional[int]]]:
    out: List[Tuple[str, Optional[int]]] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            chat, t = part.split(":", 1)
            out.append((chat.strip(), _as_int(t)))
        else:
            out.append((part, None))
    return out

MULTI_TARGETS = _parse_targets(TARGETS_ENV)

# If no multi-targets provided, fall back to the legacy single target
if not MULTI_TARGETS and TARGET_CHAT:
    MULTI_TARGETS = [(TARGET_CHAT, DEFAULT_TOPIC_ID)]

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
    if MULTI_TARGETS:
        log.info("Targets: %s", MULTI_TARGETS)

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
    Copy/forward a single message from SOURCE_CHAT -> to_peer.
    Uses top_msg_id (forum topic) when raw requests are available.
    """
    await ensure_connected()
    dest = to_peer
    top_id = _as_int(topic_id)

    # Try CopyMessages first
    if CopyMessages is not None:
        try:
            log.info("Using raw CopyMessages to %s with top_msg_id=%s", dest, top_id)
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

    # Then try ForwardMessages
    if ForwardMessages is not None:
        try:
            log.info("Using raw ForwardMessages to %s with top_msg_id=%s", dest, top_id)
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

    # Fallback: high-level forward (no top_msg_id support)
    log.error("No raw request available; forwarding to %s without topic_id.", dest)
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

    # Determine which message_id to use
    if isinstance(payload, dict) and "message_id" in payload:
        message_id = int(payload["message_id"])
    elif isinstance(payload, dict) and payload.get("slot") == "auto":
        if len(ROTATION_IDS) != 12:
            raise HTTPException(status_code=400, detail="ROTATION_12_IDS must contain exactly 12 IDs")
        message_id = int(ROTATION_IDS[current_slot_idx()])
    else:
        raise HTTPException(status_code=400, detail='Provide {"message_id":N} or {"slot":"auto"}')

    # Build targets
    targets = list(MULTI_TARGETS)

    # Optional override (rare): payload.targets = [{"chat":"@name","topic_id":123}, ...]
    if isinstance(payload, dict) and isinstance(payload.get("targets"), list):
        tmp: List[Tuple[str, Optional[int]]] = []
        for t in payload["targets"]:
            chat = (t or {}).get("chat")
            if not chat:
                continue
            tmp.append((chat, _as_int((t or {}).get("topic_id"))))
        if tmp:
            targets = tmp

    # Legacy: if someone passes "topic_id" in payload and only a single target exists,
    # apply it to that single target (kept for backwards compatibility)
    if isinstance(payload, dict) and "topic_id" in payload and len(targets) == 1:
        targets = [(targets[0][0], _as_int(payload["topic_id"]))]

    if not targets:
        raise HTTPException(status_code=400, detail="No targets configured")

    # Send to all targets
    results = []
    for chat, t_id in targets:
        try:
            res = await copy_message(message_id, to_peer=chat, topic_id=t_id)
            results.append({"chat": chat, "topic_id": t_id, "result": str(res)})
        except Exception as e:
            log.exception("Send failed for %s (topic %s): %s", chat, t_id, e)
            results.append({"chat": chat, "topic_id": t_id, "error": str(e)})

    return {"ok": True, "copied_message_id": message_id, "results": results}
