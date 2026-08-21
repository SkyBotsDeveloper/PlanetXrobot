import asyncio
import inspect
import re
import subprocess
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Set

from ntgcalls import FFmpegError
from pytgcalls import ffmpeg as pytgcalls_ffmpeg
from pytgcalls.types.stream import media_stream as pytgcalls_media_stream
from pytgcalls.types import Cache as PytgCallsCache


_PATCHES_APPLIED = False
_SUPPORTED_FLAGS: Dict[str, Set[str]] = {}
_SUPPORTED_FLAGS_LOCKS: Dict[str, asyncio.Lock] = {}


def _is_hrtf_ffmpeg_parameters(parameters: Optional[str]) -> bool:
    """Return whether parameters contain PlanetX's multi-input HRTF graph."""
    return bool(
        parameters
        and parameters.startswith("---start")
        and "headphone=map=FL|FR:hrir=stereo" in parameters
        and "-filter_complex" in parameters
    )


def patch_pytgcalls_hrtf_probe() -> None:
    """Keep HRTF-only FFmpeg inputs out of PyTgCalls' FFprobe validation."""
    original_check_stream = getattr(pytgcalls_ffmpeg, "check_stream", None)
    if not callable(original_check_stream):
        return
    if getattr(original_check_stream, "_planetx_hrtf_probe_patch", False):
        return

    async def compatible_check_stream(
        ffmpeg_parameters: Optional[str],
        path: str,
        stream_parameters: Any,
        before_commands: Optional[List[str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        return await original_check_stream(
            None if _is_hrtf_ffmpeg_parameters(ffmpeg_parameters) else ffmpeg_parameters,
            path,
            stream_parameters,
            before_commands,
            headers,
        )

    compatible_check_stream._planetx_hrtf_probe_patch = True
    pytgcalls_ffmpeg.check_stream = compatible_check_stream
    # MediaStream imports check_stream directly, so update that bound reference too.
    pytgcalls_media_stream.check_stream = compatible_check_stream


async def _get_supported_flags(executable: str) -> Set[str]:
    cached = _SUPPORTED_FLAGS.get(executable)
    if cached is not None:
        return cached

    lock = _SUPPORTED_FLAGS_LOCKS.setdefault(executable, asyncio.Lock())
    async with lock:
        cached = _SUPPORTED_FLAGS.get(executable)
        if cached is not None:
            return cached

        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                "-h",
                "full",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise FFmpegError(f"{executable} not installed") from exc

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (subprocess.TimeoutExpired, JSONDecodeError):
            proc.kill()
            raise

        supported = set(re.findall(r"(?m)^ *(-\w+).*?\s+", stdout.decode("utf-8")))
        supported.add("-i")
        _SUPPORTED_FLAGS[executable] = supported
        return supported


async def cached_cleanup_commands(
    commands: List[str],
    process_name: Optional[str] = None,
    blacklist: Optional[List[str]] = None,
) -> List[str]:
    if not commands:
        return commands

    supported = await _get_supported_flags(process_name or commands[0])
    blocked = set(blacklist or [])
    new_commands = []
    ignore_next = False

    for value in commands:
        if len(value) > 0:
            if value[0] == "-":
                ignore_next = value not in supported or value in blocked

            if not ignore_next:
                new_commands.append(value)
            elif value[0] != "-":
                ignore_next = False

    return new_commands


def patch_pytgcalls_cache_put() -> None:
    put = getattr(PytgCallsCache, "put", None)
    if not callable(put):
        return

    try:
        params = inspect.signature(put).parameters
    except (TypeError, ValueError):
        params = {}

    if len(params) >= 4:
        return

    original_put = put

    def compatible_put(self, chat_id: int, data: Any, persistent: bool = False) -> None:
        result = original_put(self, chat_id, data)
        if persistent:
            try:
                entry = getattr(self, "_store", {}).get(chat_id)
                if entry is not None and hasattr(entry, "time"):
                    entry.time = 0
            except Exception:
                pass
        return result

    PytgCallsCache.put = compatible_put


def apply_runtime_patches() -> None:
    global _PATCHES_APPLIED
    if _PATCHES_APPLIED:
        return

    pytgcalls_ffmpeg.cleanup_commands = cached_cleanup_commands
    patch_pytgcalls_cache_put()
    patch_pytgcalls_hrtf_probe()
    _PATCHES_APPLIED = True
