import aiohttp


PASTE_RS_URL = "https://paste.rs"
PASTE_HEADERS = {
    "Accept": "text/plain",
    "Content-Type": "text/plain; charset=utf-8",
    "User-Agent": "PlanetXrobotBot/1.0",
}


async def _paste_rs(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""

    async with aiohttp.ClientSession(headers=PASTE_HEADERS) as session:
        async with session.post(PASTE_RS_URL, data=text.encode("utf-8")) as resp:
            body = (await resp.text()).strip()
            if resp.status not in {200, 201} or not body.startswith(
                "https://paste.rs/"
            ):
                raise RuntimeError(f"Paste service returned HTTP {resp.status}.")
            return body


async def paste(content):
    return await _paste_rs(content)


async def PLANETXBIN(text):
    return await _paste_rs(text)
