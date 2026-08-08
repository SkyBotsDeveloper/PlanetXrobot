from pyrogram import filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import asyncio
import random
import requests
from PLANETXROBOT import app

SUPPORT_CHAT = "Planetx_music"
SUPPORT_BTN = InlineKeyboardMarkup(
    [[InlineKeyboardButton("ꜱᴜᴘᴘᴏʀᴛ", url=f"https://t.me/{SUPPORT_CHAT}")]]
)

NEKOS_BEST_API_BASE = "https://nekos.best/api/v2"
NEKOS_BEST_CUTE_CATEGORIES = ("cuddle", "hug", "happy", "pat")
NEKOS_BEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "PlanetXrobotBot (https://SkyBotsDeveloper/PlanetXrobot)",
}


def _fetch_nekos_best_url(*categories: str) -> str:
    last_error = None
    for category in categories:
        try:
            response = requests.get(
                f"{NEKOS_BEST_API_BASE}/{category}",
                headers=NEKOS_BEST_HEADERS,
                timeout=12,
            )
            response.raise_for_status()
            data = response.json()
            url = ((data.get("results") or [{}])[0].get("url") or "").strip()
            if url:
                return url
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Nekos animation lookup failed: {last_error}")


@app.on_message(filters.command("wish"))
async def wish(_, m):
    if len(m.command) < 2:
        return await m.reply_text("❌ ᴀᴅᴅ ʏᴏᴜʀ ᴡɪꜱʜ ʙᴀʙʏ 🥀!")

    try:
        url = await asyncio.to_thread(_fetch_nekos_best_url, "happy")
    except Exception:
        return await m.reply_text("⚠️ Couldn't fetch animation, try again later.")

    text = m.text.split(None, 1)[1]
    wish_count = random.randint(1, 100)
    name = m.from_user.first_name or "User"

    caption = (
        f"✨ ʜᴇʏ {name}!\n"
        f"🪄 ʏᴏᴜʀ ᴡɪꜱʜ: {text}\n"
        f"📊 ᴘᴏꜱꜱɪʙɪʟɪᴛʏ: {wish_count}%"
    )

    await app.send_animation(
        chat_id=m.chat.id,
        animation=url,
        caption=caption,
        reply_markup=SUPPORT_BTN,
    )


@app.on_message(filters.command("cute"))
async def cute(_, message):
    user = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    mention = f"[{user.first_name}](tg://user?id={user.id})"
    percent = random.randint(1, 100)

    caption = f"🍑 {mention} ɪꜱ {percent}% ᴄᴜᴛᴇ ʙᴀʙʏ 🥀"

    try:
        animation_url = await asyncio.to_thread(
            _fetch_nekos_best_url,
            *NEKOS_BEST_CUTE_CATEGORIES,
        )
    except Exception:
        return await message.reply_text("Couldn't fetch animation, try again later.")

    await app.send_animation(
        chat_id=message.chat.id,
        animation=animation_url,
        caption=caption,
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=SUPPORT_BTN,
        reply_to_message_id=(
            getattr(message.reply_to_message, "id", None)
            or getattr(message.reply_to_message, "message_id", None)
        )
        if message.reply_to_message
        else None,
    )
