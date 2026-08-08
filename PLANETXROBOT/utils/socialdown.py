from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from yt_dlp import YoutubeDL

from PLANETXROBOT.security import validate_public_http_url


API_TIMEOUT = httpx.Timeout(18.0, connect=6.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(45.0, connect=10.0)
HTTP_LIMITS = httpx.Limits(
    max_connections=10,
    max_keepalive_connections=4,
    keepalive_expiry=20.0,
)
HTTP_HEADERS = {
    "User-Agent": (
        "VivaanXDownloader/1.0 "
        "(+https://github.com/SkyBotsDeveloper/PlanetXrobot)"
    )
}
MAX_MEDIA_FILES = 8
MAX_DOWNLOAD_BYTES = 95 * 1024 * 1024
FAILURE_COOLDOWN_SECONDS = 420
FIXTWEET_API_URL = "https://api.fxtwitter.com"
VXTWITTER_API_URL = "https://api.vxtwitter.com"
TIKWM_API_URL = "https://www.tikwm.com/api/"
X_OEMBED_URL = "https://publish.twitter.com/oembed"
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
SNAPINTA_HOME_URL = "https://snapinta.app/en"
SNAPINTA_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SNAPINTA_AJAX_HEADERS = {
    **SNAPINTA_PAGE_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://snapinta.app",
    "Referer": SNAPINTA_HOME_URL,
    "X-Requested-With": "XMLHttpRequest",
}

INSTAGRAM_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "instagr.am",
    "www.instagr.am",
}
X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
FACEBOOK_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "fb.watch",
    "www.fb.watch",
}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
SNAPCHAT_HOSTS = {
    "snapchat.com",
    "www.snapchat.com",
    "story.snapchat.com",
}
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

PLATFORM_HOSTS = {
    "instagram": INSTAGRAM_HOSTS,
    "x": X_HOSTS,
    "facebook": FACEBOOK_HOSTS,
    "youtube": YOUTUBE_HOSTS,
    "snapchat": SNAPCHAT_HOSTS,
    "tiktok": TIKTOK_HOSTS,
}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
X_STATUS_PATTERN = re.compile(r"/(?:i/web/)?status(?:es)?/(\d+)", re.IGNORECASE)
CONTENT_DISPOSITION_FILENAME = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)')
META_TAG_PATTERN = re.compile(
    r"<meta[^>]+(?:property|name)\s*=\s*['\"]([^'\"]+)['\"][^>]+content\s*=\s*['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)
TITLE_TAG_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
URL_TEXT_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
SNAPINTA_VAR_PATTERN = re.compile(
    r"\b(k_url_search|k_exp|k_token)\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
HTML_MEDIA_META_KEYS = (
    ("og:video:secure_url", "video"),
    ("og:video:url", "video"),
    ("og:video", "video"),
    ("twitter:player:stream", "video"),
    ("twitter:player", "video"),
    ("og:image:secure_url", "photo"),
    ("og:image:url", "photo"),
    ("og:image", "photo"),
    ("twitter:image:src", "photo"),
    ("twitter:image", "photo"),
)
_STRATEGY_COOLDOWNS: dict[str, float] = {}


class SocialDownloadError(RuntimeError):
    pass


@dataclass(slots=True)
class SocialMediaItem:
    url: str
    kind: str
    filename_hint: str = ""
    content: str = ""


@dataclass(slots=True)
class SocialDownloadBundle:
    title: str
    source: str
    items: list[SocialMediaItem]
    note_text: str = ""


@dataclass(slots=True)
class PageMetadata:
    title: str
    description: str
    site_name: str
    items: list[SocialMediaItem]
    note_text: str


def _prune_cooldowns():
    now = time.monotonic()
    expired = [key for key, until in _STRATEGY_COOLDOWNS.items() if until <= now]
    for key in expired:
        _STRATEGY_COOLDOWNS.pop(key, None)


def _on_cooldown(key: str) -> bool:
    _prune_cooldowns()
    return _STRATEGY_COOLDOWNS.get(key, 0.0) > time.monotonic()


def _mark_cooldown(key: str, seconds: int = FAILURE_COOLDOWN_SECONDS):
    _STRATEGY_COOLDOWNS[key] = time.monotonic() + seconds


def _clean_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def _safe_filename(value: str | None, default: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]+', "_", _clean_text(value) or default).strip(" .")
    return (cleaned or default)[:160]


def _clean_html_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return _clean_text(unescape(text))


def _strip_urls_from_text(value: str | None) -> str:
    return _clean_text(URL_TEXT_PATTERN.sub("", str(value or "")))


class _AnchorLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._anchor_stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "a":
            return
        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        self._anchor_stack.append(
            {
                "href": attr_map.get("href", ""),
                "title": attr_map.get("title", ""),
                "class": attr_map.get("class", ""),
                "text_parts": [],
            }
        )

    def handle_data(self, data: str):
        if self._anchor_stack:
            self._anchor_stack[-1]["text_parts"].append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() != "a" or not self._anchor_stack:
            return
        anchor = self._anchor_stack.pop()
        self.links.append(
            {
                "href": str(anchor.get("href") or ""),
                "title": _clean_text(anchor.get("title")),
                "class": _clean_text(anchor.get("class")),
                "text": _clean_text(" ".join(anchor.get("text_parts") or [])),
            }
        )


def _guess_kind(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in VIDEO_EXTENSIONS or content_type.startswith("video/"):
        return "video"
    if suffix in PHOTO_EXTENSIONS or content_type.startswith("image/"):
        return "photo"
    if suffix in AUDIO_EXTENSIONS or content_type.startswith("audio/"):
        return "audio"
    return "document"


def _ensure_extension(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix
    if suffix:
        return filename
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    extension = mimetypes.guess_extension(media_type) if media_type else ""
    if extension:
        return f"{filename}{extension}"
    return filename


def _extract_filename(headers: httpx.Headers, url: str, fallback: str) -> str:
    disposition = headers.get("content-disposition") or ""
    match = CONTENT_DISPOSITION_FILENAME.search(disposition)
    if match:
        return _safe_filename(unquote(match.group(1).strip().strip('"')), fallback)

    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    if name:
        return _safe_filename(name, fallback)
    return _safe_filename(fallback, "media.bin")


def _validate_source_url(url: str, platform: str) -> str:
    hosts = PLATFORM_HOSTS.get(platform)
    if not hosts:
        raise SocialDownloadError("Unsupported platform.")
    return validate_public_http_url(
        url,
        allowed_hosts=hosts,
        allow_subdomains=True,
    )


def _extract_x_status_id(url: str) -> str:
    match = X_STATUS_PATTERN.search(url)
    if not match:
        raise SocialDownloadError("Invalid X/Twitter status URL.")
    return match.group(1)


def _extract_x_screen_name(url: str) -> str:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[1].lower() == "status":
        handle = path_parts[0].lstrip("@")
        if handle and handle.lower() != "i":
            return handle
    return ""


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout: httpx.Timeout | float | None = None,
    **kwargs,
):
    if timeout is not None and "timeout" not in kwargs:
        kwargs["timeout"] = timeout
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def _extract_html_metadata(html: str) -> tuple[str, str, str, dict[str, list[str]]]:
    snippet = html[:350000]
    metas: dict[str, str] = {}
    meta_values: dict[str, list[str]] = {}
    for key, value in META_TAG_PATTERN.findall(snippet):
        normalized = str(key or "").strip().lower()
        cleaned = _clean_text(unescape(value))
        if not normalized:
            continue
        meta_values.setdefault(normalized, [])
        if cleaned and cleaned not in meta_values[normalized]:
            meta_values[normalized].append(cleaned)
        if normalized not in metas:
            metas[normalized] = _clean_html_text(value)

    title_match = TITLE_TAG_PATTERN.search(snippet)
    title = (
        metas.get("og:title")
        or metas.get("twitter:title")
        or _clean_html_text(title_match.group(1) if title_match else "")
    )
    description = (
        metas.get("og:description")
        or metas.get("twitter:description")
        or metas.get("description")
    )
    site_name = metas.get("og:site_name") or metas.get("application-name") or ""
    return title, description, site_name, meta_values


def _build_page_metadata_from_html(source_url: str, html: str) -> PageMetadata:
    title, description, site_name, meta_values = _extract_html_metadata(html)
    description = _strip_urls_from_text(description)

    items: list[SocialMediaItem] = []
    seen_urls: set[str] = set()
    title_hint = _safe_filename(title or site_name or "Post", "Post")

    for meta_key, kind in HTML_MEDIA_META_KEYS:
        for raw_url in meta_values.get(meta_key, []):
            candidate = _clean_text(raw_url)
            if not candidate:
                continue
            joined = urljoin(source_url, candidate)
            try:
                safe_url = validate_public_http_url(joined, allow_subdomains=True)
            except Exception:
                continue
            if safe_url in seen_urls:
                continue
            seen_urls.add(safe_url)
            items.append(SocialMediaItem(url=safe_url, kind=kind, filename_hint=title_hint))
            if len(items) >= MAX_MEDIA_FILES:
                break
        if len(items) >= MAX_MEDIA_FILES:
            break

    lines: list[str] = []
    if site_name:
        lines.append(f"Platform: {site_name}")
    if title:
        lines.append(f"Title: {title}")
    if description and description != title:
        lines.extend(["", description])

    note_text = "\n".join(lines).strip()
    return PageMetadata(
        title=title,
        description=description,
        site_name=site_name,
        items=items,
        note_text=note_text if len(note_text) >= 20 else "",
    )


async def _build_page_note(client: httpx.AsyncClient, source_url: str) -> str:
    try:
        response = await client.get(
            source_url,
            timeout=API_TIMEOUT,
            headers={
                **HTTP_HEADERS,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()
    except Exception:
        return ""

    content_type = str(response.headers.get("content-type") or "").lower()
    if "html" not in content_type:
        return ""

    return _build_page_metadata_from_html(source_url, response.text).note_text


async def _build_page_metadata_bundle(
    client: httpx.AsyncClient,
    source_url: str,
    platform: str,
) -> SocialDownloadBundle | None:
    try:
        response = await client.get(
            source_url,
            timeout=API_TIMEOUT,
            headers={
                **HTTP_HEADERS,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()
    except Exception:
        return None

    content_type = str(response.headers.get("content-type") or "").lower()
    if "html" not in content_type:
        return None

    metadata = _build_page_metadata_from_html(source_url, response.text)
    if not metadata.items and not metadata.note_text:
        return None

    title = _safe_filename(
        metadata.title or metadata.site_name or f"{platform.title()} Post",
        f"{platform.title()} Post",
    )
    return SocialDownloadBundle(
        title=title,
        source="Page Metadata",
        items=metadata.items,
        note_text=metadata.note_text,
    )


def _append_note(bundle: SocialDownloadBundle, note_text: str) -> SocialDownloadBundle:
    note = str(note_text or "").strip()
    if not note:
        return bundle
    if bundle.note_text:
        return bundle
    return SocialDownloadBundle(title=bundle.title, source=bundle.source, items=bundle.items, note_text=note)


async def _build_youtube_oembed_note(client: httpx.AsyncClient, source_url: str) -> str:
    try:
        data = await _request_json(
            client,
            "GET",
            YOUTUBE_OEMBED_URL,
            timeout=API_TIMEOUT,
            headers=HTTP_HEADERS,
            params={"url": source_url, "format": "json"},
        )
    except Exception:
        return ""

    title = _clean_text(data.get("title"))
    author_name = _clean_text(data.get("author_name"))
    author_url = _clean_text(data.get("author_url"))
    provider_name = _clean_text(data.get("provider_name"))
    title = _strip_urls_from_text(title)
    author_name = _strip_urls_from_text(author_name)
    lines: list[str] = []
    if provider_name:
        lines.append(f"Platform: {provider_name}")
    if title:
        lines.append(f"Title: {title}")
    if author_name:
        lines.append(f"Author: {author_name}")
    return "\n".join(lines).strip()


def _extract_snapinta_config(html: str) -> tuple[str, str, str]:
    values = {
        key.lower(): unescape(value)
        for key, value in SNAPINTA_VAR_PATTERN.findall(str(html or "")[:350000])
    }
    token = _clean_text(values.get("k_token"))
    expiry = _clean_text(values.get("k_exp"))
    search_path = _clean_text(values.get("k_url_search")) or "/api/ajaxSearch"
    if not token or not expiry:
        raise SocialDownloadError("Snapinta session token was not found.")
    return urljoin(SNAPINTA_HOME_URL, search_path), token, expiry


def _decode_snapinta_filename(media_url: str) -> str:
    token = (parse_qs(urlparse(media_url).query).get("token") or [""])[0]
    parts = token.split(".")
    if len(parts) < 2:
        return ""

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8", "replace"))
    except Exception:
        return ""

    return _safe_filename(data.get("filename"), "")


def _snapinta_kind_from_link(link: dict[str, str], filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"

    text = " ".join(
        [
            link.get("title", ""),
            link.get("text", ""),
            link.get("class", ""),
            filename,
        ]
    ).lower()
    if "video" in text:
        return "video"
    if "image" in text or "photo" in text:
        return "photo"
    if "audio" in text or "music" in text:
        return "audio"
    return _guess_kind(filename, "")


def _build_snapinta_bundle(source_url: str, response_html: str) -> SocialDownloadBundle:
    parser = _AnchorLinkParser()
    parser.feed(str(response_html or ""))

    items: list[SocialMediaItem] = []
    seen_urls: set[str] = set()
    for link in parser.links:
        href = _clean_text(link.get("href"))
        label = " ".join(
            [
                link.get("title", ""),
                link.get("text", ""),
                link.get("class", ""),
            ]
        ).lower()
        if not href or "download" not in label:
            continue

        media_url = urljoin(SNAPINTA_HOME_URL, href)
        parsed = urlparse(media_url)
        if parsed.netloc.lower().endswith("snapinta.app") and parsed.path in {"", "/", "/en"}:
            continue
        try:
            safe_url = validate_public_http_url(media_url, allow_subdomains=True)
        except Exception:
            continue
        if safe_url in seen_urls:
            continue

        filename = _decode_snapinta_filename(safe_url)
        if not filename:
            fallback = "instagram_video.mp4" if "video" in label else "instagram_media"
            filename = _safe_filename(fallback, "instagram_media")
        kind = _snapinta_kind_from_link(link, filename)
        if kind not in {"video", "photo", "audio"}:
            continue

        seen_urls.add(safe_url)
        items.append(SocialMediaItem(url=safe_url, kind=kind, filename_hint=filename))
        if len(items) >= MAX_MEDIA_FILES:
            break

    if _is_instagram_video_url(source_url):
        video_items = [item for item in items if item.kind == "video"]
        if video_items:
            items = video_items

    if not items:
        raise SocialDownloadError("Snapinta returned no downloadable Instagram media.")

    title = "Instagram Reel" if _is_instagram_video_url(source_url) else "Instagram Media"
    return SocialDownloadBundle(
        title=title,
        source="Snapinta",
        items=items[:MAX_MEDIA_FILES],
    )


async def _fetch_instagram_via_snapinta(
    client: httpx.AsyncClient,
    source_url: str,
) -> SocialDownloadBundle:
    page_response = await client.get(
        SNAPINTA_HOME_URL,
        timeout=API_TIMEOUT,
        headers=SNAPINTA_PAGE_HEADERS,
    )
    page_response.raise_for_status()
    search_url, token, expiry = _extract_snapinta_config(page_response.text)

    payload = await _request_json(
        client,
        "POST",
        search_url,
        timeout=API_TIMEOUT,
        headers=SNAPINTA_AJAX_HEADERS,
        data={
            "k_exp": expiry,
            "k_token": token,
            "q": source_url,
            "t": "media",
            "lang": "en",
            "v": "v2",
            "html": "",
        },
    )
    if not isinstance(payload, dict):
        raise SocialDownloadError("Snapinta returned an invalid response.")
    if str(payload.get("status") or "").lower() != "ok":
        message = payload.get("mess") or payload.get("msg") or "Snapinta lookup failed."
        raise SocialDownloadError(_clean_html_text(message))

    response_html = payload.get("data")
    if not isinstance(response_html, str) or not response_html.strip():
        raise SocialDownloadError("Snapinta returned no download page.")

    return _build_snapinta_bundle(source_url, response_html)


def _pick_ytdlp_entries(info) -> list[dict]:
    if not isinstance(info, dict):
        raise SocialDownloadError("yt-dlp returned an unexpected response.")
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        picked = []
        for entry in entries:
            if isinstance(entry, dict):
                picked.append(entry)
        if picked:
            return picked[:MAX_MEDIA_FILES]
    return [info]


def _ytdlp_item_kind(item: dict) -> str:
    ext = str(item.get("ext") or "").strip().lower()
    suffix = f".{ext}" if ext else ""
    vcodec = str(item.get("vcodec") or "").lower()
    acodec = str(item.get("acodec") or "").lower()

    if vcodec and vcodec != "none":
        return "video"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if acodec and acodec != "none":
        return "audio"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    return ""


def _is_instagram_video_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/reel/" in path or "/tv/" in path


def _is_ytdlp_direct_media(item: dict) -> bool:
    media_url = str(item.get("url") or "").strip()
    protocol = str(item.get("protocol") or "").lower()
    size = int(item.get("filesize") or item.get("filesize_approx") or 0)
    if not media_url.startswith(("http://", "https://")):
        return False
    if "m3u8" in protocol:
        return False
    if size and size > MAX_DOWNLOAD_BYTES:
        return False
    return bool(_ytdlp_item_kind(item))


def _pick_ytdlp_format(entry: dict) -> dict:
    formats = entry.get("formats") or []
    direct_formats = [
        item
        for item in formats
        if isinstance(item, dict) and _is_ytdlp_direct_media(item)
    ]
    for preferred_kind in ("video", "audio", "photo"):
        for item in reversed(direct_formats):
            if _ytdlp_item_kind(item) == preferred_kind:
                return item

    if _is_ytdlp_direct_media(entry):
        return entry

    raise SocialDownloadError("yt-dlp returned no direct downloadable media URL.")


def _extract_ytdlp_bundle(source_url: str, platform: str) -> SocialDownloadBundle:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": platform != "instagram",
        "socket_timeout": 18,
        "format": "best[filesize<90M]/best[filesize_approx<90M]/best",
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    entries = _pick_ytdlp_entries(info)
    bundle_title = _safe_filename(
        info.get("title") or f"{platform.title()} Media",
        f"{platform.title()} Media",
    )
    note_parts = []
    uploader = _clean_text(info.get("uploader") or info.get("channel"))
    if bundle_title:
        note_parts.append(f"Title: {bundle_title}")
    if uploader:
        note_parts.append(f"Author: {uploader}")

    items: list[SocialMediaItem] = []
    seen_urls: set[str] = set()
    entry_errors: list[str] = []
    for index, entry in enumerate(entries, start=1):
        try:
            selected = _pick_ytdlp_format(entry)
        except SocialDownloadError as exc:
            entry_errors.append(str(exc))
            continue
        media_url = str(selected.get("url") or "").strip()
        if not media_url or media_url in seen_urls:
            continue
        seen_urls.add(media_url)

        title = _safe_filename(
            entry.get("title") or bundle_title or selected.get("format_id") or f"{platform.title()} Media",
            f"{platform.title()} Media",
        )
        ext = str(selected.get("ext") or entry.get("ext") or "").strip().lower()
        filename = _safe_filename(
            entry.get("filename")
            or entry.get("_filename")
            or f"{title}_{index}.{ext or 'mp4'}",
            f"{title}_{index}.{ext or 'mp4'}",
        )
        if ext and not Path(filename).suffix:
            filename = f"{filename}.{ext}"

        kind = _ytdlp_item_kind(selected) or _guess_kind(filename, "")
        if kind == "document":
            kind = _guess_kind(filename, "")
        items.append(SocialMediaItem(url=media_url, kind=kind, filename_hint=filename))

    if not items:
        detail = f" {'; '.join(entry_errors[:2])}" if entry_errors else ""
        raise SocialDownloadError(f"yt-dlp returned no downloadable media items.{detail}")

    return SocialDownloadBundle(
        title=bundle_title,
        source="yt-dlp",
        items=items[:MAX_MEDIA_FILES],
        note_text="\n".join(note_parts),
    )


async def _fetch_via_ytdlp(source_url: str, *, platform: str) -> SocialDownloadBundle:
    return await asyncio.to_thread(_extract_ytdlp_bundle, source_url, platform)


def _bundle_from_fixtweet_payload(data, endpoint_name: str, status_id: str) -> SocialDownloadBundle:
    if int(data.get("code") or 0) != 200:
        raise SocialDownloadError(str(data.get("message") or f"{endpoint_name} lookup failed."))

    tweet = data.get("tweet") or {}
    media = tweet.get("media") or {}
    author = tweet.get("author") or {}
    title = _safe_filename(tweet.get("text") or f"X {status_id}", f"X {status_id}")
    raw_text = (
        ((tweet.get("raw_text") or {}).get("text"))
        or tweet.get("text")
        or ""
    ).strip()
    raw_text = _strip_urls_from_text(raw_text)
    author_name = _clean_text(author.get("name")) or "Unknown"
    author_handle = _clean_text(author.get("screen_name"))
    author_line = author_name
    if author_handle:
        author_line += f" (@{author_handle})"
    note_text = (
        f"Author: {author_line}\n"
        f"\n{raw_text}\n"
    ).strip() if raw_text else ""
    items: list[SocialMediaItem] = []
    seen_urls: set[str] = set()

    def add_item(media_url: str | None, kind: str):
        url = str(media_url or "").strip()
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        items.append(SocialMediaItem(url=url, kind=kind, filename_hint=title))

    def add_media_block(payload: dict | None):
        if not isinstance(payload, dict):
            return
        media_block = payload.get("media") or {}
        for photo in media_block.get("photos") or []:
            add_item(photo.get("url"), "photo")
        for video in media_block.get("videos") or []:
            add_item(video.get("url"), "video")
        for entry in media_block.get("all") or []:
            media_type = str(entry.get("type") or "document").lower()
            add_item(entry.get("url"), "video" if media_type == "video" else "photo")
        external = media_block.get("external") or {}
        add_item(external.get("url"), "video")

    add_media_block(tweet)
    if not items:
        for nested_key, nested_label in (
            ("quote", "Quoted"),
            ("quoted_tweet", "Quoted"),
            ("retweet", "Retweeted"),
            ("retweeted_tweet", "Retweeted"),
        ):
            nested = tweet.get(nested_key)
            if not isinstance(nested, dict):
                continue
            add_media_block(nested)
            if items:
                nested_author = nested.get("author") or {}
                nested_name = _clean_text(nested_author.get("name")) or _clean_text(
                    nested_author.get("screen_name")
                )
                nested_text = _strip_urls_from_text(
                    ((nested.get("raw_text") or {}).get("text"))
                    or nested.get("text")
                    or ""
                )
                extra_lines = ["", f"{nested_label} post media attached."]
                if nested_name:
                    extra_lines.append(f"{nested_label} author: {nested_name}")
                if nested_text:
                    extra_lines.extend(["", nested_text])
                note_text = (
                    "\n".join([note_text, *extra_lines]).strip()
                    if note_text
                    else "\n".join(extra_lines).strip()
                )
                break

    if not items:
        if raw_text:
            return SocialDownloadBundle(
                title=title,
                source=endpoint_name,
                items=[
                    SocialMediaItem(
                        url="",
                        kind="text",
                        filename_hint=f"{title}.txt",
                        content=note_text,
                    )
                ],
                note_text=note_text,
            )
        raise SocialDownloadError(f"No direct media found in that X post via {endpoint_name}.")
    return SocialDownloadBundle(
        title=title,
        source=endpoint_name,
        items=items[:MAX_MEDIA_FILES],
        note_text=note_text,
    )


def _bundle_from_vxtwitter_payload(data, endpoint_name: str, status_id: str) -> SocialDownloadBundle:
    if not isinstance(data, dict):
        raise SocialDownloadError(f"{endpoint_name} returned an invalid response.")

    raw_text = _strip_urls_from_text(data.get("text"))
    author_name = _clean_text(data.get("user_name")) or "Unknown"
    author_handle = _clean_text(data.get("user_screen_name"))
    author_line = author_name
    if author_handle:
        author_line += f" (@{author_handle})"

    title = _safe_filename(raw_text or f"X {status_id}", f"X {status_id}")
    note_text = (
        f"Author: {author_line}\n"
        f"\n{raw_text}\n"
    ).strip() if raw_text else ""

    items: list[SocialMediaItem] = []
    seen_urls: set[str] = set()

    def add_item(media_url: str | None, kind: str):
        url = str(media_url or "").strip()
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        items.append(SocialMediaItem(url=url, kind=kind, filename_hint=title))

    def add_media_from_payload(payload: dict | None):
        if not isinstance(payload, dict):
            return
        combined_url = str(payload.get("combinedMediaUrl") or "").strip()
        media_urls = payload.get("mediaURLs") or []
        media_extended = payload.get("media_extended") or []

        for entry in media_extended[:MAX_MEDIA_FILES]:
            if not isinstance(entry, dict):
                continue
            media_type = str(entry.get("type") or "").lower()
            media_url = entry.get("url")
            if media_type == "video":
                add_item(media_url, "video")
            elif media_type in {"photo", "image", "gif"}:
                add_item(media_url, "photo")

        for media_url in media_urls[:MAX_MEDIA_FILES]:
            add_item(media_url, "video")

        if combined_url:
            add_item(combined_url, "video")

    add_media_from_payload(data)
    if not items:
        for nested_key, nested_label in (("qrt", "Quoted"), ("retweet", "Retweeted")):
            nested_tweet = data.get(nested_key)
            if not isinstance(nested_tweet, dict):
                continue
            add_media_from_payload(nested_tweet)
            if items:
                nested_author = _clean_text(nested_tweet.get("user_name")) or _clean_text(
                    nested_tweet.get("user_screen_name")
                )
                nested_text = _strip_urls_from_text(nested_tweet.get("text"))
                extra_lines = ["", f"{nested_label} post media attached."]
                if nested_author:
                    extra_lines.append(f"{nested_label} author: {nested_author}")
                if nested_text:
                    extra_lines.extend(["", nested_text])
                note_text = (
                    "\n".join([note_text, *extra_lines]).strip()
                    if note_text
                    else "\n".join(extra_lines).strip()
                )
                break

    if not items:
        if raw_text:
            return SocialDownloadBundle(
                title=title,
                source=endpoint_name,
                items=[
                    SocialMediaItem(
                        url="",
                        kind="text",
                        filename_hint=f"{title}.txt",
                        content=note_text,
                    )
                ],
                note_text=note_text,
            )
        raise SocialDownloadError(f"No direct media found in that X post via {endpoint_name}.")

    return SocialDownloadBundle(
        title=title,
        source=endpoint_name,
        items=items[:MAX_MEDIA_FILES],
        note_text=note_text,
    )


async def _fetch_x_via_vxtwitter(
    client: httpx.AsyncClient,
    source_url: str,
    *,
    screen_name: str | None = None,
) -> SocialDownloadBundle:
    status_id = _extract_x_status_id(source_url)
    endpoint = f"{VXTWITTER_API_URL}/i/status/{status_id}"
    if screen_name:
        endpoint = f"{VXTWITTER_API_URL}/{screen_name}/status/{status_id}"
    data = await _request_json(
        client,
        "GET",
        endpoint,
        timeout=API_TIMEOUT,
        headers=HTTP_HEADERS,
    )
    return _bundle_from_vxtwitter_payload(data, "VxTwitter", status_id)


async def _fetch_x_via_fixtweet(
    client: httpx.AsyncClient,
    source_url: str,
    *,
    screen_name: str | None = None,
) -> SocialDownloadBundle:
    status_id = _extract_x_status_id(source_url)
    endpoint = f"{FIXTWEET_API_URL}/i/status/{status_id}"
    if screen_name:
        endpoint = f"{FIXTWEET_API_URL}/{screen_name}/status/{status_id}"
    data = await _request_json(
        client,
        "GET",
        endpoint,
        timeout=API_TIMEOUT,
        headers=HTTP_HEADERS,
    )
    return _bundle_from_fixtweet_payload(data, "FixTweet", status_id)


async def _fetch_x_via_fixtweet_video(
    client: httpx.AsyncClient,
    source_url: str,
) -> SocialDownloadBundle:
    status_id = _extract_x_status_id(source_url)
    data = await _request_json(
        client,
        "GET",
        f"{FIXTWEET_API_URL}/video/status/{status_id}",
        timeout=API_TIMEOUT,
        headers=HTTP_HEADERS,
    )
    return _bundle_from_fixtweet_payload(data, "FixTweet Video", status_id)


async def _fetch_x_via_oembed(
    client: httpx.AsyncClient,
    source_url: str,
) -> SocialDownloadBundle:
    data = await _request_json(
        client,
        "GET",
        X_OEMBED_URL,
        timeout=API_TIMEOUT,
        headers=HTTP_HEADERS,
        params={"url": source_url, "omit_script": 1, "dnt": 1},
    )
    author_name = _clean_text(data.get("author_name")) or "Unknown"
    html = str(data.get("html") or "")
    text_match = re.search(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL)
    post_text = _strip_urls_from_text(_clean_html_text(text_match.group(1) if text_match else ""))
    title = _safe_filename(post_text or f"X {source_url.rsplit('/', 1)[-1]}", "X Post")
    note_text = (
        f"Author: {author_name}\n"
        f"\n{post_text}\n"
    ).strip()
    if not post_text:
        raise SocialDownloadError("X oEmbed returned no readable post text.")
    return SocialDownloadBundle(
        title=title,
        source="X oEmbed",
        items=[
            SocialMediaItem(
                url="",
                kind="text",
                filename_hint=f"{title}.txt",
                content=note_text,
            )
        ],
        note_text=note_text,
    )


async def _fetch_tiktok_via_tikwm(
    client: httpx.AsyncClient,
    source_url: str,
) -> SocialDownloadBundle:
    response = await client.post(
        TIKWM_API_URL,
        timeout=API_TIMEOUT,
        headers={
            **HTTP_HEADERS,
            "User-Agent": "Mozilla/5.0",
        },
        data={"url": source_url},
    )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code") or -1) != 0:
        raise SocialDownloadError(str(payload.get("msg") or "TikWM lookup failed."))

    data = payload.get("data") or {}
    title = _safe_filename(data.get("title") or "TikTok Video", "TikTok Video")
    title = _strip_urls_from_text(title)
    author = data.get("author") or {}
    author_name = _clean_text(author.get("nickname"))
    author_handle = _clean_text(author.get("unique_id"))
    author_line = author_name or "Unknown"
    if author_handle:
        author_line += f" (@{author_handle})"
    duration = data.get("duration")
    note_lines = [
        f"Author: {author_line}",
    ]
    if duration:
        note_lines.append(f"Duration: {duration}s")
    if title:
        note_lines.extend(["", title])
    note_text = "\n".join(note_lines).strip()

    items: list[SocialMediaItem] = []
    images = data.get("images") or []
    for image_url in images[:MAX_MEDIA_FILES]:
        if image_url:
            items.append(
                SocialMediaItem(
                    url=str(image_url),
                    kind="photo",
                    filename_hint=title,
                )
            )

    play_url = str(data.get("play") or "").strip()
    wmplay_url = str(data.get("wmplay") or "").strip()
    music_url = str(data.get("music") or "").strip()

    if not items:
        if play_url:
            items.append(SocialMediaItem(url=play_url, kind="video", filename_hint=title))
        elif wmplay_url:
            items.append(SocialMediaItem(url=wmplay_url, kind="video", filename_hint=title))

    if not items and music_url:
        items.append(SocialMediaItem(url=music_url, kind="audio", filename_hint=title))

    if not items:
        raise SocialDownloadError("TikWM returned no downloadable TikTok media.")

    return SocialDownloadBundle(
        title=title,
        source="TikWM",
        items=items[:MAX_MEDIA_FILES],
        note_text=note_text,
    )


async def get_social_bundle(platform: str, url: str) -> SocialDownloadBundle:
    safe_url = _validate_source_url(url, platform)
    failures: list[str] = []
    screen_name = _extract_x_screen_name(safe_url) if platform == "x" else ""

    async with httpx.AsyncClient(
        timeout=API_TIMEOUT,
        headers=HTTP_HEADERS,
        follow_redirects=True,
        trust_env=False,
        limits=HTTP_LIMITS,
    ) as client:
        strategies = []
        if platform == "x":
            if screen_name:
                strategies.append(
                    (
                        "VxTwitter",
                        f"vxtwitter:{screen_name}",
                        lambda: _fetch_x_via_vxtwitter(
                            client,
                            safe_url,
                            screen_name=screen_name,
                        ),
                    )
                )
                strategies.append(
                    (
                        "FixTweet",
                        f"fixtweet:{screen_name}",
                        lambda: _fetch_x_via_fixtweet(
                            client,
                            safe_url,
                            screen_name=screen_name,
                        ),
                    )
                )
            strategies.extend(
                [
                    ("VxTwitter", "vxtwitter:generic", lambda: _fetch_x_via_vxtwitter(client, safe_url)),
                    ("FixTweet", "fixtweet:generic", lambda: _fetch_x_via_fixtweet(client, safe_url)),
                    ("FixTweet Video", "fixtweet:video", lambda: _fetch_x_via_fixtweet_video(client, safe_url)),
                    ("X oEmbed", "x:oembed", lambda: _fetch_x_via_oembed(client, safe_url)),
                ]
            )
        elif platform == "tiktok":
            strategies.append(
                ("TikWM", "tiktok:tikwm", lambda: _fetch_tiktok_via_tikwm(client, safe_url))
            )
        elif platform == "instagram":
            strategies.append(
                (
                    "Snapinta",
                    "instagram:snapinta",
                    lambda: _fetch_instagram_via_snapinta(client, safe_url),
                )
            )

        strategies.append(
            (
                "yt-dlp",
                f"ytdlp:{platform}",
                lambda: _fetch_via_ytdlp(safe_url, platform=platform),
            )
        )

        for label, cooldown_key, runner in strategies:
            if _on_cooldown(cooldown_key):
                continue
            try:
                bundle = await runner()
                if (
                    platform == "instagram"
                    and _is_instagram_video_url(safe_url)
                    and not any(item.kind == "video" for item in bundle.items)
                ):
                    raise SocialDownloadError("Instagram returned only the preview image for this reel.")
                if platform == "youtube":
                    bundle = _append_note(bundle, await _build_youtube_oembed_note(client, safe_url))
                elif platform != "x":
                    bundle = _append_note(bundle, await _build_page_note(client, safe_url))
                return bundle
            except Exception as exc:
                _mark_cooldown(cooldown_key)
                failures.append(f"{label}: {exc}")

        metadata_bundle = await _build_page_metadata_bundle(client, safe_url, platform)
        if metadata_bundle:
            if (
                platform == "instagram"
                and _is_instagram_video_url(safe_url)
                and not any(item.kind == "video" for item in metadata_bundle.items)
            ):
                failures.append("Page Metadata: only the preview image was available for this reel")
            else:
                if not metadata_bundle.items and metadata_bundle.note_text:
                    return SocialDownloadBundle(
                        title=metadata_bundle.title,
                        source=metadata_bundle.source,
                        items=[
                            SocialMediaItem(
                                url="",
                                kind="text",
                                filename_hint=f"{metadata_bundle.title}.txt",
                                content=metadata_bundle.note_text,
                            )
                        ],
                        note_text=metadata_bundle.note_text,
                    )
                return metadata_bundle

    details = "\n".join(failures[:3])
    raise SocialDownloadError(
        "Downloader services are temporarily unavailable.\n"
        f"{details}"
    )


async def download_bundle_files(bundle: SocialDownloadBundle) -> tuple[str, list[tuple[str, str]]]:
    temp_dir = tempfile.mkdtemp(prefix="planetx_social_")
    downloads: list[tuple[str, str]] = []

    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT,
        headers=HTTP_HEADERS,
        follow_redirects=True,
        trust_env=False,
        limits=HTTP_LIMITS,
    ) as client:
        try:
            for index, item in enumerate(bundle.items[:MAX_MEDIA_FILES], start=1):
                if item.kind == "text" and item.content:
                    filename = _safe_filename(
                        item.filename_hint or f"{bundle.title}_{index}.txt",
                        f"{bundle.title}_{index}.txt",
                    )
                    if not filename.lower().endswith(".txt"):
                        filename += ".txt"
                    file_path = os.path.join(temp_dir, filename)
                    with open(file_path, "w", encoding="utf-8") as handle:
                        handle.write(item.content)
                    downloads.append((file_path, "document"))
                    continue

                safe_remote = validate_public_http_url(item.url, allow_subdomains=True)
                async with client.stream("GET", safe_remote) as response:
                    response.raise_for_status()
                    total_size = int(response.headers.get("content-length") or 0)
                    if total_size and total_size > MAX_DOWNLOAD_BYTES:
                        raise SocialDownloadError("Remote media is too large to send.")

                    fallback_name = f"{bundle.title}_{index}"
                    content_type = str(response.headers.get("content-type") or "")
                    filename = _extract_filename(response.headers, safe_remote, fallback_name)
                    filename = _ensure_extension(filename, content_type)
                    file_path = os.path.join(temp_dir, filename)

                    downloaded = 0
                    with open(file_path, "wb") as handle:
                        async for chunk in response.aiter_bytes(65536):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > MAX_DOWNLOAD_BYTES:
                                raise SocialDownloadError("Downloaded media is too large to send.")
                            handle.write(chunk)

                    media_kind = item.kind
                    if media_kind == "document":
                        media_kind = _guess_kind(
                            filename,
                            content_type,
                        )
                    downloads.append((file_path, media_kind))

            if not downloads:
                raise SocialDownloadError("No media could be downloaded from that link.")
            return temp_dir, downloads
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
