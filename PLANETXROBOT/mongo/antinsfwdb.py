from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import NSFW_SAFE_CACHE_TTL_SECONDS
from PLANETXROBOT.core.mongo import mongodb

_state_col = mongodb["antinsfw"]
_cache_col = mongodb["antinsfw_cache"]


async def is_antinsfw_on(chat_id: int) -> bool:
    doc = await _state_col.find_one({"_id": int(chat_id)}, {"state": 1})
    return (doc or {}).get("state") == "on"


async def set_antinsfw_state(chat_id: int, state: str) -> None:
    if state not in {"on", "off"}:
        raise ValueError("state must be 'on' or 'off'")
    await _state_col.update_one(
        {"_id": int(chat_id)},
        {"$set": {"state": state, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def get_cached_detection(file_unique_id: str) -> Optional[dict[str, Any]]:
    if not file_unique_id:
        return None

    doc = await _cache_col.find_one({"_id": file_unique_id})
    if not doc:
        return None

    if doc.get("status") == "safe":
        if NSFW_SAFE_CACHE_TTL_SECONDS <= 0:
            return None
        updated_at = doc.get("updated_at")
        if isinstance(updated_at, datetime):
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated_at > timedelta(seconds=NSFW_SAFE_CACHE_TTL_SECONDS):
                return None

    return doc


async def save_detection_cache(
    file_unique_id: str,
    media_kind: str,
    status: str,
    confidence: float,
    label: str,
) -> None:
    if not file_unique_id or status not in {"safe", "nsfw"}:
        return
    if status == "safe" and NSFW_SAFE_CACHE_TTL_SECONDS <= 0:
        return
    await _cache_col.update_one(
        {"_id": file_unique_id},
        {
            "$set": {
                "media_kind": media_kind,
                "status": status,
                "confidence": float(confidence or 0.0),
                "label": label or "",
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
