import asyncio
import re
import time
from collections import defaultdict, deque
from typing import Any

from pyrogram import StopPropagation, enums, filters
from pyrogram.errors import ChatAdminRequired, FloodWait, RPCError, UserAdminInvalid
from pyrogram.types import ChatPermissions, Message

from config import BANNED_USERS
from PLANETXROBOT import app
from PLANETXROBOT.logging import LOGGER
from PLANETXROBOT.mongo.groupmoddb import (
    add_blocklist,
    add_warn,
    approve_user,
    clear_blocklist,
    delete_note,
    get_note,
    get_rules,
    get_settings,
    get_warns,
    is_approved,
    list_blocklist,
    list_notes,
    list_approved,
    matching_blocklists,
    remove_blocklist,
    remove_warn,
    reset_rules,
    reset_warns,
    save_note,
    set_locks,
    set_rules,
    unapprove_user,
    update_settings,
)
from PLANETXROBOT.utils.decorator import admin_required
from PLANETXROBOT.utils.permissions import mention, parse_time

logger = LOGGER(__name__)

_MUTE_PERMS = ChatPermissions()
_FLOOD_BUCKETS: dict[tuple[int, int], deque[float]] = defaultdict(deque)
_BIO_CLEAN_CACHE: dict[int, float] = {}
_BIO_FETCH_LOCKS: dict[int, asyncio.Lock] = {}
_BIO_FETCH_SEMAPHORE = asyncio.Semaphore(6)
_BIO_CLEAN_CACHE_SECONDS = 8

LOCK_TYPES = {
    "all",
    "audio",
    "command",
    "contact",
    "document",
    "forward",
    "gif",
    "invitelink",
    "location",
    "media",
    "photo",
    "poll",
    "sticker",
    "text",
    "url",
    "video",
    "voice",
}
ACTION_MODES = {"ban", "kick", "mute", "delete", "warn", "nothing"}
WARN_ACTION_MODES = {"ban", "kick", "mute"}
URL_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/)", re.I)
INVITE_RE = re.compile(r"(t\.me/(joinchat/|\+)|telegram\.(me|dog)/(joinchat/|\+))", re.I)
NOTE_RE = re.compile(r"^#([A-Za-z0-9_-]{1,64})(?:\s|$)")
BIO_LINK_RE = re.compile(
    r"(?i)(?:"
    r"@[a-zA-Z0-9_][a-zA-Z0-9_]{3,31}|"
    r"t\.me/[a-zA-Z0-9_./\-]+|"
    r"telegram\.me/[a-zA-Z0-9_./\-]+|"
    r"tg\.me/[a-zA-Z0-9_./\-]+|"
    r"https?://[^\s]+|"
    r"www\.[a-zA-Z0-9.\-]+(?:[/?#][^\s]*)?|"
    r"(?:bit\.ly|ow\.ly|tinyurl\.com|short\.link|goo\.gl|is\.gd)/[a-zA-Z0-9.\-_]+|"
    r"(?:instagram|tiktok|twitter|facebook|youtube|linkedin)\.com/[a-zA-Z0-9.\-_~:/?#@!$&'()*+,;=%]+|"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
    r"(?:com|org|net|io|co|uk|app|dev|shop)(?:[/?#][^\s]*)?"
    r")"
)


@app.on_message(filters.command("rules") & filters.group & ~BANNED_USERS)
async def rules_cmd(_, message: Message):
    rules = await get_rules(message.chat.id)
    if not rules:
        return await message.reply_text("No rules have been set for this chat.")
    await message.reply_text(f"**Rules for {message.chat.title}:**\n\n{rules}", disable_web_page_preview=True)


@app.on_message(filters.command("setrules") & filters.group & ~BANNED_USERS)
@admin_required("can_change_info")
async def setrules_cmd(_, message: Message):
    text = _command_payload(message)
    if not text and message.reply_to_message:
        text = _message_text(message.reply_to_message)
    if not text:
        return await message.reply_text("Usage: /setrules <rules text> or reply with /setrules")
    await set_rules(message.chat.id, text)
    await message.reply_text("Rules updated.")


@app.on_message(filters.command(["resetrules", "clearrules"]) & filters.group & ~BANNED_USERS)
@admin_required("can_change_info")
async def resetrules_cmd(_, message: Message):
    removed = await reset_rules(message.chat.id)
    await message.reply_text("Rules cleared." if removed else "No rules were set.")


@app.on_message(filters.command("save") & filters.group & ~BANNED_USERS)
@admin_required("can_change_info")
async def save_note_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /save <name> <text> or reply with /save <name>")

    name = message.command[1].strip("#").lower()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return await message.reply_text("Note names can only use letters, numbers, underscore and dash.")

    note = _build_note(message, name)
    if not note:
        return await message.reply_text("Give note text, or reply to a message/media with /save <name>.")

    await save_note(message.chat.id, name, note)
    await message.reply_text(f"Saved note #{name}.")


@app.on_message(filters.command(["get", "note"]) & filters.group & ~BANNED_USERS)
async def get_note_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /get <name>")
    await _send_note(message, message.command[1].strip("#").lower())


@app.on_message(filters.command(["notes", "saved"]) & filters.group & ~BANNED_USERS)
async def list_notes_cmd(_, message: Message):
    names = await list_notes(message.chat.id)
    if not names:
        return await message.reply_text("No notes saved in this chat.")
    text = "**Saved notes:**\n" + "\n".join(f"- #{name}" for name in names)
    await message.reply_text(text)


@app.on_message(filters.command(["clear", "delnote"]) & filters.group & ~BANNED_USERS)
@admin_required("can_change_info")
async def clear_note_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /clear <name>")
    name = message.command[1].strip("#").lower()
    removed = await delete_note(message.chat.id, name)
    await message.reply_text(f"Deleted note #{name}." if removed else f"No note named #{name}.")


@app.on_message(filters.command(["warn", "dwarn", "swarn"]) & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def warn_cmd(client, message: Message):
    if len(message.command) == 1 and not message.reply_to_message:
        return await message.reply_text("Usage: /warn @user [reason] or reply with /warn [reason]")

    uid, name, reason = await _target_user_and_reason(client, message)
    if not uid:
        return await message.reply_text("Specify a user or reply to a user's message.")
    if await _is_protected(client, message.chat.id, uid):
        return await message.reply_text("I cannot warn admins or the group owner.")

    command = message.command[0].lower()
    if command in {"dwarn", "swarn"} and message.reply_to_message:
        try:
            await message.reply_to_message.delete()
        except RPCError:
            pass

    settings = await get_settings(message.chat.id)
    count = await add_warn(message.chat.id, uid, reason or "", message.from_user.id)
    limit = int(settings.get("warn_limit", 3) or 3)
    if command == "swarn":
        try:
            await message.delete()
        except RPCError:
            pass
    else:
        await message.reply_text(
            f"{mention(uid, name or str(uid))} warned: {reason or 'No reason given.'}\n"
            f"Warnings: {count}/{limit}"
        )

    if count >= limit:
        await reset_warns(message.chat.id, uid)
        mode = str(settings.get("warn_mode", "mute")).lower()
        duration = str(settings.get("warn_duration", "1d"))
        await _apply_user_action(client, message, uid, name or str(uid), mode, duration, "warn limit reached")


@app.on_message(filters.command(["warns", "warnings"]) & filters.group & ~BANNED_USERS)
async def warns_cmd(client, message: Message):
    uid = None
    name = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        uid, name = user.id, user.first_name
    elif len(message.command) > 1:
        try:
            user = await client.get_users(message.command[1])
            uid, name = user.id, user.first_name
        except RPCError:
            return await message.reply_text("I can't find that user.")
    elif message.from_user:
        uid, name = message.from_user.id, message.from_user.first_name

    warns = await get_warns(message.chat.id, uid)
    if not warns:
        return await message.reply_text(f"{mention(uid, name or str(uid))} has no warnings.")
    lines = [f"**Warnings for {mention(uid, name or str(uid))}:**"]
    for index, warn in enumerate(warns, start=1):
        lines.append(f"{index}. {warn.get('reason') or 'No reason given.'}")
    await message.reply_text("\n".join(lines))


@app.on_message(filters.command(["rmwarn", "unwarn"]) & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def rmwarn_cmd(client, message: Message):
    uid, name = await _target_user(client, message)
    if not uid:
        return await message.reply_text("Usage: /rmwarn @user or reply with /rmwarn")
    removed = await remove_warn(message.chat.id, uid)
    await message.reply_text(
        f"Removed one warning from {mention(uid, name or str(uid))}." if removed else "That user has no warnings."
    )


@app.on_message(filters.command(["resetwarn", "resetwarns"]) & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def resetwarn_cmd(client, message: Message):
    uid, name = await _target_user(client, message)
    if not uid:
        return await message.reply_text("Usage: /resetwarn @user or reply with /resetwarn")
    removed = await reset_warns(message.chat.id, uid)
    await message.reply_text(
        f"Reset warnings for {mention(uid, name or str(uid))}." if removed else "That user has no warnings."
    )


@app.on_message(filters.command("setwarnlimit") & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def setwarnlimit_cmd(_, message: Message):
    if len(message.command) != 2 or not message.command[1].isdigit():
        return await message.reply_text("Usage: /setwarnlimit <number>")
    limit = max(1, min(20, int(message.command[1])))
    await update_settings(message.chat.id, {"warn_limit": limit})
    await message.reply_text(f"Warn limit set to {limit}.")


@app.on_message(filters.command(["setwarnmode", "warnmode"]) & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def setwarnmode_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /setwarnmode <ban|kick|mute> [duration]")
    mode = message.command[1].lower()
    if mode not in WARN_ACTION_MODES:
        return await message.reply_text("Warn mode must be ban, kick or mute.")
    duration = message.command[2] if len(message.command) > 2 else "1d"
    if mode == "mute" and not parse_time(duration):
        return await message.reply_text("Invalid duration. Use 10m, 2h, 1d, etc.")
    await update_settings(message.chat.id, {"warn_mode": mode, "warn_duration": duration})
    await message.reply_text(f"Warn mode set to {mode} {duration if mode == 'mute' else ''}".strip() + ".")


@app.on_message(filters.command("report") & filters.group & ~BANNED_USERS)
async def report_cmd(client, message: Message):
    await _handle_report(client, message, _command_payload(message) or "No reason given.")


@app.on_message(filters.regex(r"^@admin(?:\s|$)") & filters.group & ~BANNED_USERS)
async def admin_report_cmd(client, message: Message):
    reason = _message_text(message).partition("@admin")[2].strip() or "No reason given."
    await _handle_report(client, message, reason)


async def _handle_report(client, message: Message, reason: str):
    if not message.reply_to_message:
        return await message.reply_text("Reply to a message with /report [reason].")
    if message.from_user and await _is_admin(client, message.chat.id, message.from_user.id):
        return
    settings = await get_settings(message.chat.id)
    if settings.get("reports", "on") != "on":
        return await message.reply_text("Reports are disabled in this chat.")
    if message.reply_to_message.from_user and await _is_protected(client, message.chat.id, message.reply_to_message.from_user.id):
        return await message.reply_text("Admins cannot be reported.")

    admins = await _admin_mentions(client, message.chat.id, limit=15)
    if not admins:
        return await message.reply_text("No admins found to report to.")
    reporter = message.from_user.mention if message.from_user else "Someone"
    target = message.reply_to_message.from_user.mention if message.reply_to_message.from_user else "a message"
    await message.reply_to_message.reply_text(
        f"{' '.join(admins)}\n\nReport by {reporter} against {target}\nReason: {reason}",
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("reports") & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def reports_cmd(_, message: Message):
    if len(message.command) != 2 or message.command[1].lower() not in {"on", "off"}:
        settings = await get_settings(message.chat.id)
        return await message.reply_text(f"Reports are currently {settings.get('reports', 'on')}. Usage: /reports on|off")
    state = message.command[1].lower()
    await update_settings(message.chat.id, {"reports": state})
    await message.reply_text(f"Reports turned {state}.")


@app.on_message(filters.command(["addblocklist", "blocklistadd"]) & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def addblocklist_cmd(_, message: Message):
    payload = _command_payload(message)
    if not payload:
        return await message.reply_text("Usage: /addblocklist <word or phrase> [reason]")
    trigger, reason = _split_trigger_reason(payload)
    if not trigger:
        return await message.reply_text("Give a valid trigger.")
    await add_blocklist(message.chat.id, trigger, reason)
    await message.reply_text(f"Added blocklist trigger: `{trigger}`")


@app.on_message(filters.command(["rmblocklist", "unblocklist"]) & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def rmblocklist_cmd(_, message: Message):
    payload = _command_payload(message)
    if not payload:
        return await message.reply_text("Usage: /rmblocklist <trigger>")
    removed = await remove_blocklist(message.chat.id, payload.lower())
    await message.reply_text("Blocklist trigger removed." if removed else "That trigger was not found.")


@app.on_message(filters.command(["blocklist", "blocklists"]) & filters.group & ~BANNED_USERS)
async def blocklist_cmd(_, message: Message):
    rows = await list_blocklist(message.chat.id)
    if not rows:
        return await message.reply_text("No blocklist triggers set.")
    text = "**Blocklist triggers:**\n" + "\n".join(f"- `{row['trigger']}`" for row in rows[:80])
    await message.reply_text(text)


@app.on_message(filters.command("rmblocklistall") & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def rmblocklistall_cmd(_, message: Message):
    count = await clear_blocklist(message.chat.id)
    await message.reply_text(f"Removed {count} blocklist trigger(s).")


@app.on_message(filters.command("blocklistmode") & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def blocklistmode_cmd(_, message: Message):
    if len(message.command) != 2 or message.command[1].lower() not in ACTION_MODES:
        return await message.reply_text("Usage: /blocklistmode <delete|warn|mute|kick|ban|nothing>")
    mode = message.command[1].lower()
    await update_settings(message.chat.id, {"blocklist_mode": mode})
    await message.reply_text(f"Blocklist mode set to {mode}.")


@app.on_message(filters.command(["lock", "unlock"]) & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def lock_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /lock <type> or /unlock <type>. Use /locktypes.")
    requested = [item.lower() for item in message.command[1:]]
    invalid = [item for item in requested if item not in LOCK_TYPES]
    if invalid:
        return await message.reply_text(f"Unknown lock type(s): {', '.join(invalid)}. Use /locktypes.")
    settings = await get_settings(message.chat.id)
    locks = set(settings.get("locks", []))
    if message.command[0].lower() == "lock":
        locks.update(requested)
        state = "locked"
    else:
        locks.difference_update(requested)
        state = "unlocked"
    await set_locks(message.chat.id, locks)
    await message.reply_text(f"{', '.join(requested)} {state}.")


@app.on_message(filters.command(["locks", "locktypes"]) & filters.group & ~BANNED_USERS)
async def locks_cmd(_, message: Message):
    settings = await get_settings(message.chat.id)
    active = set(settings.get("locks", []))
    if message.command[0].lower() == "locktypes":
        return await message.reply_text("**Lock types:**\n" + ", ".join(sorted(LOCK_TYPES)))
    if not active:
        return await message.reply_text("No locks enabled.")
    await message.reply_text("**Active locks:**\n" + ", ".join(sorted(active)))


@app.on_message(filters.command(["setflood", "flood"]) & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def setflood_cmd(_, message: Message):
    if len(message.command) != 2 or not message.command[1].isdigit():
        settings = await get_settings(message.chat.id)
        return await message.reply_text(
            f"Flood limit is {settings.get('flood_limit', 0)} messages. Usage: /setflood <number>, 0 disables."
        )
    limit = max(0, min(50, int(message.command[1])))
    await update_settings(message.chat.id, {"flood_limit": limit})
    await message.reply_text("Antiflood disabled." if limit == 0 else f"Antiflood limit set to {limit} messages.")


@app.on_message(filters.command("setfloodtimer") & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def setfloodtimer_cmd(_, message: Message):
    if len(message.command) != 2 or not message.command[1].isdigit():
        return await message.reply_text("Usage: /setfloodtimer <seconds>")
    seconds = max(3, min(120, int(message.command[1])))
    await update_settings(message.chat.id, {"flood_window": seconds})
    await message.reply_text(f"Antiflood window set to {seconds} seconds.")


@app.on_message(filters.command("setfloodmode") & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def setfloodmode_cmd(_, message: Message):
    if len(message.command) < 2 or message.command[1].lower() not in {"ban", "kick", "mute"}:
        return await message.reply_text("Usage: /setfloodmode <ban|kick|mute> [duration]")
    mode = message.command[1].lower()
    duration = message.command[2] if len(message.command) > 2 else "10m"
    if mode == "mute" and not parse_time(duration):
        return await message.reply_text("Invalid duration. Use 10m, 2h, 1d, etc.")
    await update_settings(message.chat.id, {"flood_mode": mode, "flood_duration": duration})
    await message.reply_text(f"Antiflood mode set to {mode} {duration if mode == 'mute' else ''}".strip() + ".")


@app.on_message(filters.command("clearflood") & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def clearflood_cmd(_, message: Message):
    for key in list(_FLOOD_BUCKETS):
        if key[0] == message.chat.id:
            _FLOOD_BUCKETS.pop(key, None)
    await message.reply_text("Antiflood counters cleared for this chat.")


@app.on_message(filters.command("antibio") & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def antibio_cmd(_, message: Message):
    settings = await get_settings(message.chat.id)
    if len(message.command) != 2 or message.command[1].lower() not in {"on", "off", "status"}:
        state = str(settings.get("antibio", "off")).lower()
        return await message.reply_text(f"Anti-bio is currently {state}. Usage: /antibio on|off")

    action = message.command[1].lower()
    if action == "status":
        state = str(settings.get("antibio", "off")).lower()
        return await message.reply_text(f"Anti-bio is currently {state}.")

    await update_settings(message.chat.id, {"antibio": action})
    if action == "on":
        text = (
            "Anti-bio enabled. Non-admin, non-approved users with links in their bio "
            "will have messages deleted until the bio is clean."
        )
    else:
        text = "Anti-bio disabled."
    await message.reply_text(text)


@app.on_message(filters.command("approve") & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def approve_cmd(client, message: Message):
    uid, name = await _target_user(client, message)
    if not uid:
        return await message.reply_text("Usage: /approve @user or reply with /approve")
    await approve_user(message.chat.id, uid, message.from_user.id if message.from_user else None)
    _FLOOD_BUCKETS.pop((message.chat.id, uid), None)
    _BIO_CLEAN_CACHE.pop(uid, None)
    await message.reply_text(
        f"{mention(uid, name or str(uid))} approved. This user now bypasses locks, blocklists, flood and anti-bio."
    )


@app.on_message(filters.command(["unapprove", "disapprove"]) & filters.group & ~BANNED_USERS)
@admin_required("can_restrict_members")
async def unapprove_cmd(client, message: Message):
    uid, name = await _target_user(client, message)
    if not uid:
        return await message.reply_text("Usage: /unapprove @user or reply with /unapprove")
    removed = await unapprove_user(message.chat.id, uid)
    _BIO_CLEAN_CACHE.pop(uid, None)
    await message.reply_text(
        f"{mention(uid, name or str(uid))} unapproved." if removed else "That user is not approved."
    )


@app.on_message(filters.command(["approved", "approvedlist"]) & filters.group & ~BANNED_USERS)
async def approved_cmd(client, message: Message):
    users = await list_approved(message.chat.id)
    if not users:
        return await message.reply_text("No approved users in this chat.")

    lines = ["**Approved users:**"]
    for index, user_id in enumerate(users[:80], start=1):
        try:
            user = await client.get_users(user_id)
            name = user.first_name or str(user_id)
        except RPCError:
            name = str(user_id)
        lines.append(f"{index}. {mention(user_id, name)} (`{user_id}`)")
    if len(users) > 80:
        lines.append(f"...and {len(users) - 80} more.")
    await message.reply_text("\n".join(lines), disable_web_page_preview=True)


@app.on_message(filters.group & ~BANNED_USERS, group=-90)
async def antibio_guard_watcher(client, message: Message):
    if not message.from_user:
        return
    if await _is_admin(client, message.chat.id, message.from_user.id):
        return
    if await is_approved(message.chat.id, message.from_user.id):
        return

    settings = await get_settings(message.chat.id)
    if str(settings.get("antibio", "off")).lower() != "on":
        return

    if await _user_has_bio_link(client, message.from_user.id):
        await _delete_message(message)
        raise StopPropagation


@app.on_message(filters.group & ~BANNED_USERS, group=10)
async def group_moderation_watcher(client, message: Message):
    if not message.from_user:
        return

    note_match = NOTE_RE.match(_message_text(message))
    if note_match:
        await _send_note(message, note_match.group(1).lower())

    if await _is_admin(client, message.chat.id, message.from_user.id):
        return

    if await is_approved(message.chat.id, message.from_user.id):
        return

    settings = await get_settings(message.chat.id)

    lock_type = _matching_lock(message, set(settings.get("locks", [])))
    if lock_type:
        await _delete_message(message)
        return

    text = _message_text(message)
    if text:
        matches = await matching_blocklists(message.chat.id, text)
        if matches:
            trigger = str(matches[0].get("trigger", ""))
            reason = str(matches[0].get("reason", "")) or f"blocklist trigger: {trigger}"
            await _handle_blocklist_match(client, message, settings, reason)
            return

    await _handle_flood(client, message, settings)


def _command_payload(message: Message) -> str:
    text = message.text or message.caption or ""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _message_text(message: Message) -> str:
    return message.text or message.caption or ""


def _flood_wait_seconds(exc: FloodWait) -> int:
    return int(getattr(exc, "value", getattr(exc, "x", 0)) or 0)


def _bio_fetch_lock(user_id: int) -> asyncio.Lock:
    lock = _BIO_FETCH_LOCKS.get(user_id)
    if lock is not None:
        return lock

    if len(_BIO_FETCH_LOCKS) > 5000:
        for cached_user_id, cached_lock in list(_BIO_FETCH_LOCKS.items()):
            if not cached_lock.locked():
                _BIO_FETCH_LOCKS.pop(cached_user_id, None)
            if len(_BIO_FETCH_LOCKS) <= 4500:
                break

    lock = asyncio.Lock()
    _BIO_FETCH_LOCKS[user_id] = lock
    return lock


async def _fetch_user_bio(client, user_id: int) -> str | None:
    for attempt in range(2):
        try:
            async with _BIO_FETCH_SEMAPHORE:
                chat = await client.get_chat(user_id)
            return getattr(chat, "bio", None) or ""
        except FloodWait as exc:
            wait_for = _flood_wait_seconds(exc)
            if wait_for <= 20 and attempt == 0:
                await asyncio.sleep(wait_for + 1)
                continue
            logger.warning("Skipping bio check for user=%s after FloodWait=%ss", user_id, wait_for)
            return None
        except RPCError as exc:
            logger.warning("Unable to fetch bio for user=%s: %s", user_id, exc)
            return None
    return None


async def _user_has_bio_link(client, user_id: int) -> bool:
    now = time.monotonic()
    clean_until = _BIO_CLEAN_CACHE.get(user_id)
    if clean_until and clean_until > now:
        return False
    if clean_until:
        _BIO_CLEAN_CACHE.pop(user_id, None)

    async with _bio_fetch_lock(user_id):
        now = time.monotonic()
        clean_until = _BIO_CLEAN_CACHE.get(user_id)
        if clean_until and clean_until > now:
            return False
        if clean_until:
            _BIO_CLEAN_CACHE.pop(user_id, None)

        bio = await _fetch_user_bio(client, user_id)
        if bio is None:
            return False

        if BIO_LINK_RE.search(bio):
            _BIO_CLEAN_CACHE.pop(user_id, None)
            return True

        _BIO_CLEAN_CACHE[user_id] = time.monotonic() + _BIO_CLEAN_CACHE_SECONDS
        return False


def _build_note(message: Message, name: str) -> dict[str, Any] | None:
    reply = message.reply_to_message
    payload = _command_payload(message)
    if payload.startswith(name):
        payload = payload[len(name):].strip()

    source = reply or message
    text = payload if payload and not reply else _message_text(source)
    note: dict[str, Any] = {"type": "text", "text": text or ""}

    for attr, note_type in (
        ("photo", "photo"),
        ("sticker", "sticker"),
        ("animation", "animation"),
        ("video", "video"),
        ("document", "document"),
        ("audio", "audio"),
        ("voice", "voice"),
    ):
        media = getattr(source, attr, None)
        if media:
            note = {
                "type": note_type,
                "file_id": media.file_id,
                "text": text or _message_text(source),
            }
            break

    if note["type"] == "text" and not note["text"]:
        return None
    return note


async def _send_note(message: Message, name: str) -> None:
    note = await get_note(message.chat.id, name)
    if not note:
        return
    note_type = note.get("type", "text")
    text = note.get("text", "")
    file_id = note.get("file_id")
    if note_type == "text":
        await message.reply_text(text, disable_web_page_preview=True)
    elif note_type == "photo":
        await message.reply_photo(file_id, caption=text or None)
    elif note_type == "sticker":
        await message.reply_sticker(file_id)
    elif note_type == "animation":
        await message.reply_animation(file_id, caption=text or None)
    elif note_type == "video":
        await message.reply_video(file_id, caption=text or None)
    elif note_type == "document":
        await message.reply_document(file_id, caption=text or None)
    elif note_type == "audio":
        await message.reply_audio(file_id, caption=text or None)
    elif note_type == "voice":
        await message.reply_voice(file_id, caption=text or None)


async def _target_user(client, message: Message) -> tuple[int | None, str | None]:
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        return user.id, user.first_name
    if len(message.command) > 1:
        try:
            user = await client.get_users(message.command[1])
            return user.id, user.first_name
        except RPCError:
            return None, None
    return None, None


async def _target_user_and_reason(client, message: Message) -> tuple[int | None, str | None, str | None]:
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        reason = _command_payload(message) or None
        return user.id, user.first_name, reason
    if len(message.command) < 2:
        return None, None, None
    try:
        user = await client.get_users(message.command[1])
    except RPCError:
        return None, None, None
    reason = _message_text(message).partition(message.command[1])[2].strip() or None
    return user.id, user.first_name, reason


async def _is_admin(client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except RPCError:
        return False
    return member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)


async def _is_protected(client, chat_id: int, user_id: int) -> bool:
    return await _is_admin(client, chat_id, user_id)


async def _admin_mentions(client, chat_id: int, limit: int = 15) -> list[str]:
    mentions: list[str] = []
    async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        user = member.user
        if not user or user.is_bot:
            continue
        mentions.append(mention(user.id, user.first_name))
        if len(mentions) >= limit:
            break
    return mentions


def _split_trigger_reason(payload: str) -> tuple[str, str]:
    if "|" in payload:
        trigger, reason = payload.split("|", 1)
        return trigger.strip(), reason.strip()
    if payload.startswith('"') and '"' in payload[1:]:
        end = payload.find('"', 1)
        return payload[1:end].strip(), payload[end + 1:].strip()
    return payload.strip(), ""


async def _handle_blocklist_match(client, message: Message, settings: dict[str, Any], reason: str) -> None:
    mode = str(settings.get("blocklist_mode", "delete")).lower()
    if mode == "nothing":
        return
    if mode == "delete":
        await _delete_message(message)
        return
    if mode == "warn":
        await _warn_from_watcher(client, message, settings, reason)
        return
    await _delete_message(message)
    await _apply_user_action(client, message, message.from_user.id, message.from_user.first_name, mode, "10m", reason)


async def _warn_from_watcher(client, message: Message, settings: dict[str, Any], reason: str) -> None:
    count = await add_warn(message.chat.id, message.from_user.id, reason, 0)
    limit = int(settings.get("warn_limit", 3) or 3)
    await _delete_message(message)
    await client.send_message(
        message.chat.id,
        f"{message.from_user.mention} warned: {reason}\nWarnings: {count}/{limit}",
    )
    if count >= limit:
        await reset_warns(message.chat.id, message.from_user.id)
        await _apply_user_action(
            client,
            message,
            message.from_user.id,
            message.from_user.first_name,
            str(settings.get("warn_mode", "mute")),
            str(settings.get("warn_duration", "1d")),
            "warn limit reached",
        )


async def _handle_flood(client, message: Message, settings: dict[str, Any]) -> None:
    limit = int(settings.get("flood_limit", 0) or 0)
    if limit <= 0:
        return
    window = int(settings.get("flood_window", 10) or 10)
    key = (message.chat.id, message.from_user.id)
    bucket = _FLOOD_BUCKETS[key]
    now = time.monotonic()
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    bucket.append(now)
    if len(bucket) <= limit:
        return
    bucket.clear()
    await _delete_message(message)
    mode = str(settings.get("flood_mode", "mute"))
    duration = str(settings.get("flood_duration", "10m"))
    await _apply_user_action(client, message, message.from_user.id, message.from_user.first_name, mode, duration, "flooding")


def _matching_lock(message: Message, locks: set[str]) -> str | None:
    if not locks:
        return None
    text = _message_text(message)
    checks = {
        "all": True,
        "audio": bool(message.audio),
        "command": text.startswith("/"),
        "contact": bool(message.contact),
        "document": bool(message.document),
        "forward": bool(getattr(message, "forward_date", None) or getattr(message, "forward_from", None)),
        "gif": bool(message.animation or (message.document and message.document.mime_type == "image/gif")),
        "invitelink": bool(INVITE_RE.search(text)),
        "location": bool(message.location or message.venue),
        "media": bool(message.media),
        "photo": bool(message.photo),
        "poll": bool(message.poll),
        "sticker": bool(message.sticker),
        "text": bool(text and not text.startswith("/") and not message.media),
        "url": bool(URL_RE.search(text)),
        "video": bool(message.video or message.video_note),
        "voice": bool(message.voice),
    }
    for lock in sorted(locks):
        if checks.get(lock):
            return lock
    return None


async def _delete_message(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except RPCError:
        return False


async def _apply_user_action(
    client,
    message: Message,
    user_id: int,
    name: str,
    mode: str,
    duration: str,
    reason: str,
) -> None:
    mode = mode.lower()
    try:
        if mode == "ban":
            await client.ban_chat_member(message.chat.id, user_id)
            await client.send_message(message.chat.id, f"{mention(user_id, name)} banned: {reason}")
        elif mode == "kick":
            await client.ban_chat_member(message.chat.id, user_id)
            await client.unban_chat_member(message.chat.id, user_id)
            await client.send_message(message.chat.id, f"{mention(user_id, name)} kicked: {reason}")
        elif mode == "mute":
            delta = parse_time(duration) or parse_time("10m")
            until = int(time.time() + delta.total_seconds()) if delta else None
            await client.restrict_chat_member(message.chat.id, user_id, _MUTE_PERMS, until_date=until)
            await client.send_message(message.chat.id, f"{mention(user_id, name)} muted for {duration}: {reason}")
    except (ChatAdminRequired, UserAdminInvalid) as exc:
        logger.warning("Moderation action failed in chat=%s user=%s: %s", message.chat.id, user_id, exc)
        try:
            await client.send_message(message.chat.id, "I need the required admin permission to apply that action.")
        except RPCError:
            pass
