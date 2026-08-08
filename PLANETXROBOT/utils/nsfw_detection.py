import asyncio
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from config import (
    HF_TOKEN,
    NSFW_FALLBACK_ENABLED,
    NSFW_FALLBACK_MIN_LOCAL_SCORE,
    NSFW_FALLBACK_PROVIDER,
    NSFW_FALLBACK_THRESHOLD,
    NSFW_FALLBACK_TIMEOUT,
    NSFW_FALLBACK_URL,
    NSFW_MAX_VIDEO_FRAMES,
    NSFW_MODEL_NAME,
    NSFW_THRESHOLD,
    NSFW_TORCH_DEVICE,
    NSFW_TORCH_NUM_THREADS,
    NSFW_VIDEO_FRAME_INTERVAL,
)
from PLANETXROBOT.logging import LOGGER

logger = LOGGER(__name__)


@dataclass(frozen=True)
class NsfwDetectionResult:
    status: str
    confidence: float = 0.0
    label: str = ""
    reason: str = ""
    frames_checked: int = 0
    provider: str = "local"

    @property
    def is_nsfw(self) -> bool:
        return self.status == "nsfw"


class LocalNsfwDetector:
    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._torch = None
        self._device = "cpu"
        self._id2label: dict[int, str] = {}
        self._load_lock = threading.Lock()
        self._predict_lock = threading.Lock()

    def detect_file(self, path: Path, media_kind: str) -> NsfwDetectionResult:
        try:
            if media_kind == "image":
                return self.detect_image(path)
            if media_kind == "gif":
                return self.detect_gif(path)
            if media_kind == "video":
                return self.detect_video(path)
            return NsfwDetectionResult("skipped", reason=f"unsupported_media_kind:{media_kind}")
        except ImportError as exc:
            logger.warning("NSFW detector dependency missing: %s", exc)
            return NsfwDetectionResult("error", reason=f"model_dependency_missing:{exc}")
        except Exception as exc:
            logger.exception("NSFW detection failed for %s", path)
            return NsfwDetectionResult("error", reason=str(exc))

    def detect_image(self, path: Path) -> NsfwDetectionResult:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            is_nsfw, confidence, label = self._predict(image)
        return NsfwDetectionResult(
            "nsfw" if is_nsfw else "safe",
            confidence=confidence,
            label=label,
            frames_checked=1,
        )

    def detect_gif(self, path: Path) -> NsfwDetectionResult:
        from PIL import Image, ImageOps, ImageSequence

        with Image.open(path) as image:
            frame_indexes = self._sample_indexes(getattr(image, "n_frames", 1), NSFW_MAX_VIDEO_FRAMES)
            best_confidence = 0.0
            best_label = ""
            frames_checked = 0

            for index, frame in enumerate(ImageSequence.Iterator(image)):
                if index not in frame_indexes:
                    continue
                frame = ImageOps.exif_transpose(frame).convert("RGB")
                is_nsfw, confidence, label = self._predict(frame)
                frames_checked += 1
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_label = label
                if is_nsfw:
                    return NsfwDetectionResult(
                        "nsfw",
                        confidence=confidence,
                        label=label,
                        frames_checked=frames_checked,
                    )

        if frames_checked == 0:
            return NsfwDetectionResult("skipped", reason="gif_decode_failed")
        return NsfwDetectionResult(
            "safe",
            confidence=best_confidence,
            label=best_label,
            frames_checked=frames_checked,
        )

    def detect_video(self, path: Path) -> NsfwDetectionResult:
        import cv2
        from PIL import Image

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return NsfwDetectionResult("error", reason="video_open_failed")

        try:
            positions = self._sample_video_positions(cap)
            if not positions:
                return NsfwDetectionResult("skipped", reason="no_video_frames")

            best_confidence = 0.0
            best_label = ""
            frames_checked = 0

            for position in positions:
                cap.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = Image.fromarray(rgb)
                is_nsfw, confidence, label = self._predict(image)
                frames_checked += 1
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_label = label
                if is_nsfw:
                    return NsfwDetectionResult(
                        "nsfw",
                        confidence=confidence,
                        label=label,
                        frames_checked=frames_checked,
                    )

            if frames_checked == 0:
                return NsfwDetectionResult("skipped", reason="video_decode_failed")
            return NsfwDetectionResult(
                "safe",
                confidence=best_confidence,
                label=best_label,
                frames_checked=frames_checked,
            )
        finally:
            cap.release()

    def _predict(self, image: Any) -> tuple[bool, float, str]:
        self._ensure_loaded()
        assert self._model is not None
        assert self._processor is not None
        assert self._torch is not None

        with self._predict_lock:
            inputs = self._processor(images=image, return_tensors="pt")
            inputs = {
                key: value.to(self._device)
                for key, value in inputs.items()
                if hasattr(value, "to")
            }
            with self._torch.inference_mode():
                outputs = self._model(**inputs)
                probabilities = self._torch.softmax(outputs.logits, dim=-1)[0].detach().cpu()

        scores = [
            (self._id2label.get(index, str(index)), float(probabilities[index]))
            for index in range(len(probabilities))
        ]
        nsfw_score = 0.0
        nsfw_labels: list[str] = []

        for label, score in scores:
            if self._is_nsfw_label(label):
                nsfw_score += score
                nsfw_labels.append(label)

        if not nsfw_labels and len(scores) == 2 and self._id2label.get(0) in {"normal", "safe", "sfw"}:
            nsfw_labels = [self._id2label.get(1, "nsfw")]
            nsfw_score = scores[1][1]

        if nsfw_labels:
            confidence = nsfw_score
            label = "+".join(nsfw_labels)
        else:
            top_label, _ = max(scores, key=lambda item: item[1])
            confidence = 0.0
            label = top_label

        return confidence >= NSFW_THRESHOLD and bool(nsfw_labels), confidence, label

    def _ensure_loaded(self) -> None:
        if self._model is not None and self._processor is not None:
            return

        with self._load_lock:
            if self._model is not None and self._processor is not None:
                return

            import torch
            from transformers import AutoImageProcessor, AutoModelForImageClassification

            if NSFW_TORCH_NUM_THREADS:
                torch.set_num_threads(NSFW_TORCH_NUM_THREADS)

            self._torch = torch
            self._device = self._select_device()
            logger.info("Loading NSFW model %s on %s", NSFW_MODEL_NAME, self._device)
            self._processor = AutoImageProcessor.from_pretrained(NSFW_MODEL_NAME)
            self._model = AutoModelForImageClassification.from_pretrained(NSFW_MODEL_NAME)
            self._model.eval()
            self._model.to(self._device)
            self._id2label = {
                int(key): str(value).lower()
                for key, value in getattr(self._model.config, "id2label", {}).items()
            }
            logger.info("NSFW model loaded with labels: %s", self._id2label)

    def _select_device(self) -> str:
        requested = NSFW_TORCH_DEVICE
        if requested == "auto":
            return "cuda" if self._torch.cuda.is_available() else "cpu"
        if requested.startswith("cuda") and not self._torch.cuda.is_available():
            logger.warning("NSFW_TORCH_DEVICE=%s requested but CUDA is unavailable; using CPU", requested)
            return "cpu"
        return requested

    def _sample_video_positions(self, cap: Any) -> list[int]:
        import cv2

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)

        if frame_count <= 0:
            return [0]

        positions = set(self._evenly_spaced_indexes(frame_count, NSFW_MAX_VIDEO_FRAMES))
        if fps > 0:
            step = max(1, int(fps * NSFW_VIDEO_FRAME_INTERVAL))
            positions.update(range(0, frame_count, step))
        return self._cap_positions(sorted(positions), frame_count, NSFW_MAX_VIDEO_FRAMES)

    @staticmethod
    def _sample_indexes(frame_count: int, max_items: int) -> set[int]:
        return set(LocalNsfwDetector._evenly_spaced_indexes(frame_count, max_items))

    @staticmethod
    def _evenly_spaced_indexes(frame_count: int, max_items: int) -> list[int]:
        frame_count = max(1, int(frame_count or 1))
        max_items = min(max(1, max_items), frame_count)
        if max_items == 1:
            return [frame_count // 2]
        stride = (frame_count - 1) / float(max_items - 1)
        return [int(round(i * stride)) for i in range(max_items)]

    @staticmethod
    def _cap_positions(positions: list[int], frame_count: int, max_items: int) -> list[int]:
        bounded = sorted(set(max(0, min(frame_count - 1, pos)) for pos in positions))
        if len(bounded) <= max_items:
            return bounded
        indexes = LocalNsfwDetector._evenly_spaced_indexes(len(bounded), max_items)
        return [bounded[index] for index in indexes]

    @staticmethod
    def _is_nsfw_label(label: str) -> bool:
        normalized = label.lower().replace("-", "_").replace(" ", "_")
        safe_labels = {"safe", "sfw", "normal", "neutral", "not_nsfw"}
        if normalized in safe_labels:
            return False
        return any(
            token in normalized
            for token in ("nsfw", "porn", "hentai", "sexy", "explicit", "nude", "sexual")
        )


_detector = LocalNsfwDetector()


async def detect_nsfw_file(path: Path, media_kind: str) -> NsfwDetectionResult:
    local_result = await asyncio.to_thread(_detector.detect_file, path, media_kind)
    fallback_result = await _detect_with_online_fallback(path, media_kind, local_result)
    if fallback_result:
        if fallback_result.is_nsfw or local_result.status != "safe":
            return fallback_result
    return local_result


async def _detect_with_online_fallback(
    path: Path,
    media_kind: str,
    local_result: NsfwDetectionResult,
) -> Optional[NsfwDetectionResult]:
    if not _should_call_fallback(path, media_kind, local_result):
        return None

    try:
        if NSFW_FALLBACK_PROVIDER == "huggingface":
            return await _call_huggingface(path, media_kind)
        if NSFW_FALLBACK_PROVIDER == "naas":
            return await _call_naas(path)
        logger.warning("Unknown NSFW_FALLBACK_PROVIDER=%s", NSFW_FALLBACK_PROVIDER)
        return None
    except Exception as exc:
        logger.warning("Online NSFW fallback failed for %s: %s", path, exc)
        return None


def _should_call_fallback(path: Path, media_kind: str, local_result: NsfwDetectionResult) -> bool:
    if not NSFW_FALLBACK_ENABLED:
        return False
    if NSFW_FALLBACK_PROVIDER == "huggingface" and not HF_TOKEN:
        return False
    if NSFW_FALLBACK_PROVIDER != "huggingface" and not NSFW_FALLBACK_URL:
        return False
    if media_kind not in {"image", "gif", "video"}:
        return False
    if local_result.status == "nsfw":
        return False
    if local_result.status == "safe" and local_result.confidence < NSFW_FALLBACK_MIN_LOCAL_SCORE:
        return False
    return path.exists()


async def _call_huggingface(path: Path, media_kind: str) -> Optional[NsfwDetectionResult]:
    image_bytes, content_type = await asyncio.to_thread(_fallback_image_bytes, path, media_kind)
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": content_type,
    }
    async with httpx.AsyncClient(timeout=NSFW_FALLBACK_TIMEOUT) as http:
        response = await http.post(_fallback_url(), content=image_bytes, headers=headers)
        response.raise_for_status()
        payload = response.json()
    return _parse_huggingface_response(payload)


async def _call_naas(path: Path) -> Optional[NsfwDetectionResult]:
    with path.open("rb") as file:
        files = {"image": (path.name, file, "application/octet-stream")}
        async with httpx.AsyncClient(timeout=NSFW_FALLBACK_TIMEOUT) as http:
            response = await http.post(_fallback_url(), files=files)
            response.raise_for_status()
            payload = response.json()
    return _parse_naas_response(payload)


def _fallback_url() -> str:
    if NSFW_FALLBACK_PROVIDER == "huggingface" and not NSFW_FALLBACK_URL:
        model_id = quote(NSFW_MODEL_NAME, safe="/")
        return f"https://router.huggingface.co/hf-inference/models/{model_id}"

    parts = urlsplit(NSFW_FALLBACK_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if NSFW_FALLBACK_PROVIDER == "naas":
        query.setdefault("fast", "1")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _fallback_image_bytes(path: Path, media_kind: str) -> tuple[bytes, str]:
    if media_kind == "image":
        return path.read_bytes(), "application/octet-stream"

    if media_kind == "gif":
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            image.seek(min(getattr(image, "n_frames", 1) // 2, max(getattr(image, "n_frames", 1) - 1, 0)))
            frame = ImageOps.exif_transpose(image).convert("RGB")
            buffer = BytesIO()
            frame.save(buffer, format="JPEG", quality=92)
            return buffer.getvalue(), "image/jpeg"

    if media_kind == "video":
        import cv2

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError("video_open_failed")
        try:
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if frame_count > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_count // 2)
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("video_decode_failed")
            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                raise RuntimeError("video_frame_encode_failed")
            return encoded.tobytes(), "image/jpeg"
        finally:
            cap.release()

    raise RuntimeError(f"unsupported_media_kind:{media_kind}")


def _parse_huggingface_response(payload: Any) -> Optional[NsfwDetectionResult]:
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, list):
        raise RuntimeError("unexpected_huggingface_response")

    nsfw_score = 0.0
    nsfw_labels: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").lower()
        score = _normalize_confidence(item.get("score", 0.0))
        if LocalNsfwDetector._is_nsfw_label(label):
            nsfw_score += score
            nsfw_labels.append(label)

    is_nsfw = bool(nsfw_labels) and nsfw_score >= NSFW_FALLBACK_THRESHOLD
    return NsfwDetectionResult(
        "nsfw" if is_nsfw else "safe",
        confidence=nsfw_score,
        label=f"huggingface:{'+'.join(nsfw_labels) or 'safe'}",
        reason="online_fallback:huggingface",
        provider="online",
    )


def _parse_naas_response(payload: dict[str, Any]) -> Optional[NsfwDetectionResult]:
    status = str(payload.get("status", "")).upper()
    if status == "NOQUOTA":
        logger.warning("Online NSFW fallback quota exhausted")
        return None
    if status != "OK":
        reason = str(payload.get("reason") or f"online_status:{status or 'unknown'}")
        raise RuntimeError(reason)

    data = payload.get("data") or {}
    confidence = _normalize_confidence(data.get("confidence", 0.0))
    classification = str(data.get("classification") or "").lower()
    nsfw = bool(data.get("nsfw")) or bool(data.get("porn")) or classification in {"nsfw", "porn"}
    nsfw = nsfw and confidence >= NSFW_FALLBACK_THRESHOLD

    return NsfwDetectionResult(
        "nsfw" if nsfw else "safe",
        confidence=confidence,
        label=f"online:{classification or 'nsfw'}",
        reason="online_fallback",
        provider="online",
    )


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence > 1.0:
        confidence /= 100.0
    return max(0.0, min(1.0, confidence))
