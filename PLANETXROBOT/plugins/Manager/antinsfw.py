import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pyrogram import enums, filters
from pyrogram.errors import ChatAdminRequired, RPCError
from pyrogram.types import Message

from config import (
    BANNED_USERS,
    NSFW_DOWNLOAD_TIMEOUT,
    NSFW_INFERENCE_CONCURRENCY,
    NSFW_MAX_IMAGE_SIZE_MB,
    NSFW_MAX_VIDEO_SIZE_MB,
    NSFW_PROCESSING_TIMEOUT,
)
from PLANETXROBOT import app
from PLANETXROBOT.logging import LOGGER
from PLANETXROBOT.mongo.antinsfwdb import (
    get_cached_detection,
    is_antinsfw_on,
    save_detection_cache,
    set_antinsfw_state,
)
from PLANETXROBOT.utils.decorator import admin_required
from PLANETXROBOT.utils.nsfw_detection import detect_nsfw_file

logger = LOGGER(__name__)

TEMP_DIR = Path("downloads") / "antinsfw"
MAX_IMAGE_BYTES = NSFW_MAX_IMAGE_SIZE_MB * 1024 * 1024
MAX_VIDEO_BYTES = NSFW_MAX_VIDEO_SIZE_MB * 1024 * 1024
_detection_gate = asyncio.Semaphore(max(1, NSFW_INFERENCE_CONCURRENCY))


@dataclass(frozen=True)
class MediaInfo:
    kind: str
    file_id: str
    file_unique_id: str
    file_size: int
    suffix: str

    @property
    def size_limit(self) -> int:
        return MAX_VIDEO_BYTES if self.kind == "video" else MAX_IMAGE_BYTES


@app.on_message(filters.command("antinsfw") & filters.group & ~BANNED_USERS)
@admin_required("can_delete_messages")
async def antinsfw_cmd(client, message: Message):
    usage = (
        "**Usage:**\n"
        "/antinsfw on - enable NSFW media auto-delete\n"
        "/antinsfw off - disable NSFW media auto-delete\n"
        "/antinsfw status - show current status"
    )
    if len(message.command) != 2:
        return await message.reply_text(usage)

    action = message.command[1].lower()
    if action not in {"on", "off", "status"}:
        return await message.reply_text(usage)

    if action == "status":
        enabled = await is_antinsfw_on(message.chat.id)
        state = "enabled" if enabled else "disabled"
        return await message.reply_text(f"**Anti-NSFW is currently {state} in this chat.**")

    if action == "on" and not await _bot_can_delete(client, message.chat.id):
        return await message.reply_text(
            "**I need admin permission to delete messages before Anti-NSFW can be enabled.**"
        )

    await set_antinsfw_state(message.chat.id, action)
    state = "enabled" if action == "on" else "disabled"
    await message.reply_text(f"**Anti-NSFW {state} for this chat.**")


@app.on_message(
    filters.group
    & ~BANNED_USERS
    & (filters.photo | filters.sticker | filters.animation | filters.video | filters.document),
    group=8,
)
async def scan_nsfw_media(client, message: Message):
    if not await is_antinsfw_on(message.chat.id):
        return

    info = _extract_media_info(message)
    if not info:
        return

    cached = await get_cached_detection(info.file_unique_id)
    if cached:
        if cached.get("status") == "nsfw":
            await _delete_and_warn(client, message)
        return

    if info.file_size and info.file_size > info.size_limit:
        return

    downloaded_path = None
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        target = TEMP_DIR / f"{message.chat.id}_{message.id}_{uuid4().hex}{info.suffix}"
        downloaded_path = await asyncio.wait_for(
            message.download(file_name=str(target)),
            timeout=NSFW_DOWNLOAD_TIMEOUT,
        )
        if not downloaded_path:
            return

        path = Path(downloaded_path)
        async with _detection_gate:
            result = await asyncio.wait_for(
                detect_nsfw_file(path, info.kind),
                timeout=NSFW_PROCESSING_TIMEOUT,
            )

        await save_detection_cache(
            info.file_unique_id,
            info.kind,
            result.status,
            result.confidence,
            result.label,
        )

        if result.is_nsfw:
            await _delete_and_warn(client, message)
    except asyncio.TimeoutError:
        logger.warning("Anti-NSFW scan timed out for chat=%s message=%s", message.chat.id, message.id)
    except Exception as exc:
        logger.warning("Anti-NSFW scan failed for chat=%s message=%s: %s", message.chat.id, message.id, exc)
    finally:
        if downloaded_path and os.path.exists(downloaded_path):
            try:
                os.remove(downloaded_path)
            except OSError:
                pass


def _extract_media_info(message: Message) -> MediaInfo | None:
    if message.photo:
        return MediaInfo("image", message.photo.file_id, message.photo.file_unique_id, message.photo.file_size or 0, ".jpg")

    if message.sticker:
        sticker = message.sticker
        if getattr(sticker, "is_animated", False):
            return None
        if getattr(sticker, "is_video", False) or getattr(sticker, "mime_type", "") == "video/webm":
            return MediaInfo("video", sticker.file_id, sticker.file_unique_id, sticker.file_size or 0, ".webm")
        return MediaInfo("image", sticker.file_id, sticker.file_unique_id, sticker.file_size or 0, ".webp")

    if message.animation:
        animation = message.animation
        suffix = _suffix_from_name(getattr(animation, "file_name", ""), ".mp4")
        return MediaInfo("video", animation.file_id, animation.file_unique_id, animation.file_size or 0, suffix)

    if message.video:
        video = message.video
        suffix = _suffix_from_name(getattr(video, "file_name", ""), ".mp4")
        return MediaInfo("video", video.file_id, video.file_unique_id, video.file_size or 0, suffix)

    if message.document:
        document = message.document
        mime_type = (document.mime_type or "").lower()
        suffix = _suffix_from_name(document.file_name or "", ".bin")
        if mime_type == "image/gif" or suffix == ".gif":
            return MediaInfo("gif", document.file_id, document.file_unique_id, document.file_size or 0, ".gif")
        if mime_type.startswith("image/"):
            return MediaInfo("image", document.file_id, document.file_unique_id, document.file_size or 0, suffix)
        if mime_type.startswith("video/"):
            return MediaInfo("video", document.file_id, document.file_unique_id, document.file_size or 0, suffix)

    return None


def _suffix_from_name(file_name: str, default: str) -> str:
    suffix = Path(file_name or "").suffix.lower()
    if suffix and 2 <= len(suffix) <= 8:
        return suffix
    return default


async def _delete_and_warn(client, message: Message) -> None:
    mention = _sender_mention(message)
    deleted = True
    try:
        await message.delete()
    except (ChatAdminRequired, RPCError):
        deleted = False

    if deleted:
        text = f"{mention}, NSFW media detected and removed. Please do not send it again."
    else:
        text = f"{mention}, NSFW media detected. I need delete-message permission to remove it."

    try:
        await client.send_message(message.chat.id, text, disable_web_page_preview=True)
    except RPCError:
        pass


def _sender_mention(message: Message) -> str:
    if message.from_user:
        return message.from_user.mention
    if message.sender_chat:
        return message.sender_chat.title
    return "User"


async def _bot_can_delete(client, chat_id: int) -> bool:
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
    except RPCError:
        return False
    if member.status == enums.ChatMemberStatus.OWNER:
        return True
    privileges = getattr(member, "privileges", None)
    return bool(member.status == enums.ChatMemberStatus.ADMINISTRATOR and privileges and privileges.can_delete_messages)
