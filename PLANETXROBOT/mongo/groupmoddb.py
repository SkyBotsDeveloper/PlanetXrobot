from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from PLANETXROBOT.core.mongo import mongodb

_rules = mongodb["group_rules"]
_notes = mongodb["group_notes"]
_warns = mongodb["group_warns"]
_settings = mongodb["group_mod_settings"]
_blocklists = mongodb["group_blocklists"]

DEFAULT_SETTINGS = {
    "warn_limit": 3,
    "warn_mode": "mute",
    "warn_duration": "1d",
    "reports": "on",
    "blocklist_mode": "delete",
    "flood_limit": 0,
    "flood_window": 10,
    "flood_mode": "mute",
    "flood_duration": "10m",
    "locks": [],
}


async def get_settings(chat_id: int) -> dict[str, Any]:
    doc = await _settings.find_one({"_id": int(chat_id)})
    settings = DEFAULT_SETTINGS.copy()
    if doc:
        settings.update({key: value for key, value in doc.items() if key != "_id"})
    return settings


async def update_settings(chat_id: int, values: dict[str, Any]) -> None:
    await _settings.update_one(
        {"_id": int(chat_id)},
        {"$set": {**values, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def set_rules(chat_id: int, text: str) -> None:
    await _rules.update_one(
        {"_id": int(chat_id)},
        {"$set": {"text": text, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def get_rules(chat_id: int) -> str | None:
    doc = await _rules.find_one({"_id": int(chat_id)}, {"text": 1})
    return (doc or {}).get("text")


async def reset_rules(chat_id: int) -> bool:
    result = await _rules.delete_one({"_id": int(chat_id)})
    return result.deleted_count > 0


async def save_note(chat_id: int, name: str, note: dict[str, Any]) -> None:
    await _notes.update_one(
        {"chat_id": int(chat_id), "name": name.lower()},
        {"$set": {**note, "name": name.lower(), "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def get_note(chat_id: int, name: str) -> dict[str, Any] | None:
    return await _notes.find_one({"chat_id": int(chat_id), "name": name.lower()})


async def list_notes(chat_id: int) -> list[str]:
    names: list[str] = []
    async for doc in _notes.find({"chat_id": int(chat_id)}, {"name": 1}).sort("name", 1):
        names.append(str(doc["name"]))
    return names


async def delete_note(chat_id: int, name: str) -> bool:
    result = await _notes.delete_one({"chat_id": int(chat_id), "name": name.lower()})
    return result.deleted_count > 0


async def add_warn(chat_id: int, user_id: int, reason: str, admin_id: int) -> int:
    now = datetime.now(timezone.utc)
    warning = {"reason": reason or "No reason given.", "admin_id": int(admin_id), "date": now}
    await _warns.update_one(
        {"chat_id": int(chat_id), "user_id": int(user_id)},
        {"$push": {"warns": warning}, "$set": {"updated_at": now}},
        upsert=True,
    )
    return await warn_count(chat_id, user_id)


async def get_warns(chat_id: int, user_id: int) -> list[dict[str, Any]]:
    doc = await _warns.find_one({"chat_id": int(chat_id), "user_id": int(user_id)}, {"warns": 1})
    return list((doc or {}).get("warns", []))


async def warn_count(chat_id: int, user_id: int) -> int:
    return len(await get_warns(chat_id, user_id))


async def remove_warn(chat_id: int, user_id: int) -> bool:
    doc = await _warns.find_one({"chat_id": int(chat_id), "user_id": int(user_id)}, {"warns": 1})
    warns = list((doc or {}).get("warns", []))
    if not warns:
        return False
    warns.pop()
    if warns:
        await _warns.update_one(
            {"chat_id": int(chat_id), "user_id": int(user_id)},
            {"$set": {"warns": warns, "updated_at": datetime.now(timezone.utc)}},
        )
    else:
        await _warns.delete_one({"chat_id": int(chat_id), "user_id": int(user_id)})
    return True


async def reset_warns(chat_id: int, user_id: int) -> bool:
    result = await _warns.delete_one({"chat_id": int(chat_id), "user_id": int(user_id)})
    return result.deleted_count > 0


async def add_blocklist(chat_id: int, trigger: str, reason: str = "") -> None:
    await _blocklists.update_one(
        {"chat_id": int(chat_id), "trigger": trigger.lower()},
        {
            "$set": {
                "trigger": trigger.lower(),
                "reason": reason,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


async def remove_blocklist(chat_id: int, trigger: str) -> bool:
    result = await _blocklists.delete_one({"chat_id": int(chat_id), "trigger": trigger.lower()})
    return result.deleted_count > 0


async def clear_blocklist(chat_id: int) -> int:
    result = await _blocklists.delete_many({"chat_id": int(chat_id)})
    return int(result.deleted_count)


async def list_blocklist(chat_id: int) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    async for doc in _blocklists.find({"chat_id": int(chat_id)}).sort("trigger", 1):
        docs.append(doc)
    return docs


async def matching_blocklists(chat_id: int, text: str) -> list[dict[str, Any]]:
    lowered = text.lower()
    matches: list[dict[str, Any]] = []
    async for doc in _blocklists.find({"chat_id": int(chat_id)}):
        trigger = str(doc.get("trigger", "")).lower()
        if trigger and trigger in lowered:
            matches.append(doc)
    return matches


async def set_locks(chat_id: int, locks: Iterable[str]) -> None:
    await update_settings(chat_id, {"locks": sorted(set(locks))})
