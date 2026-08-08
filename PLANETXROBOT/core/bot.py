import sys
from html import escape

import aiohttp
from pyrogram import Client, errors
from pyrogram.enums import ChatMemberStatus

import config
from ..logging import LOGGER


class JARVIS(Client):
    def __init__(self):
        super().__init__(
            name="PlanetXrobot",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            in_memory=True,
            workers=48,
            max_concurrent_transmissions=7,
        )
        LOGGER(__name__).info("Bot client initialized.")

    async def _bot_api(self, method: str, payload: dict):
        timeout = aiohttp.ClientTimeout(total=20)
        url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                data = await response.json(content_type=None)
                status = response.status

        if not data.get("ok"):
            reason = data.get("description") or f"HTTP {status}"
            raise RuntimeError(reason)
        return data.get("result")

    async def _send_logger_startup(self, text: str) -> None:
        try:
            await self.send_message(config.LOGGER_ID, text)
            return
        except (errors.ChannelInvalid, errors.PeerIdInvalid):
            await self._bot_api(
                "sendMessage",
                {
                    "chat_id": config.LOGGER_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            LOGGER(__name__).warning(
                "LOGGER_ID was sent through Bot API fallback because Pyrogram "
                "could not resolve the private chat by numeric id."
            )

    async def _is_logger_admin(self) -> bool:
        try:
            member = await self.get_chat_member(config.LOGGER_ID, self.id)
            owner_status = getattr(
                ChatMemberStatus,
                "OWNER",
                ChatMemberStatus.ADMINISTRATOR,
            )
            return member.status in {
                ChatMemberStatus.ADMINISTRATOR,
                owner_status,
            }
        except (errors.ChannelInvalid, errors.PeerIdInvalid):
            result = await self._bot_api(
                "getChatMember",
                {"chat_id": config.LOGGER_ID, "user_id": self.id},
            )
            return str(result.get("status") or "").lower() in {
                "administrator",
                "creator",
            }

    async def start(self):
        await super().start()
        me = await self.get_me()
        self.username, self.id = me.username, me.id
        self.name = f"{me.first_name} {me.last_name or ''}".strip()
        self.mention = me.mention

        username = f"@{self.username}" if self.username else "N/A"
        startup_text = (
            f"<u><b>Bot started</b></u>\n\n"
            f"ID: <code>{self.id}</code>\n"
            f"Name: {escape(self.name)}\n"
            f"Username: {escape(username)}"
        )

        try:
            await self._send_logger_startup(startup_text)
        except Exception as exc:
            LOGGER(__name__).error(
                "Bot has failed to access the log group.\n"
                f"Reason: {type(exc).__name__}: {exc}"
            )
            sys.exit()

        try:
            if not await self._is_logger_admin():
                LOGGER(__name__).error(
                    "Promote the bot as admin in the log group/channel."
                )
                sys.exit()
        except Exception as e:
            LOGGER(__name__).error(f"Could not check admin status: {e}")
            sys.exit()

        LOGGER(__name__).info(f"Music Bot started as {self.name} (@{self.username})")
