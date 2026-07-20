import io
import logging
import secrets
import warnings
import json
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

import config
from vercel.blob import AsyncBlobClient
from vercel.blob.errors import BlobError
from vercel._internal.blob import core as _blob_core

logger = logging.getLogger("booking")
Image.MAX_IMAGE_PIXELS = 20_000_000


def _enhance_blob_error_logging() -> None:
    original_map_blob_error = _blob_core.map_blob_error

    def map_blob_error_with_response(response):
        code, err = original_map_blob_error(response)
        text = None
        json_data = None
        try:
            text = response.text
        except Exception:
            text = None
        content_type = response.headers.get("content-type", "")
        if text is not None and "json" in content_type.lower():
            try:
                json_data = response.json()
            except Exception:
                json_data = None
        try:
            err.http_status_code = response.status_code
            err.http_response_text = text
            err.http_response_json = json_data
        except Exception:
            pass
        return code, err

    _blob_core.map_blob_error = map_blob_error_with_response


def _sanitize_exception_dict(exc_dict: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in exc_dict.items():
        lower_key = key.lower()
        if "token" in lower_key or "auth" in lower_key:
            sanitized[key] = "<redacted>"
            continue
        if isinstance(value, dict):
            sanitized[key] = _sanitize_exception_dict(value)
            continue
        if isinstance(value, (list, tuple)):
            sanitized[key] = [
                _sanitize_exception_dict(item) if isinstance(item, dict) else "<redacted>" if isinstance(item, str) and "token" in item.lower() else item
                for item in value
            ]
            continue
        sanitized[key] = value
    return sanitized


def _safe_exception_info(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    http_response = None
    if hasattr(exc, "http_response_text") or hasattr(exc, "http_response_json"):
        http_response = {
            "status_code": getattr(exc, "http_status_code", None),
            "text": getattr(exc, "http_response_text", None),
            "json": getattr(exc, "http_response_json", None),
        }
    if response is not None:
        try:
            response_text = response.text
        except Exception:
            response_text = None
        try:
            response_json = response.json()
        except Exception:
            response_json = None
        http_response = {
            "status_code": getattr(response, "status_code", None),
            "headers": {
                k: v
                for k, v in getattr(response, "headers", {}).items()
                if k.lower() != "authorization"
            },
            "text": response_text[:2000] + "..." if isinstance(response_text, str) and len(response_text) > 2000 else response_text,
            "json": response_json,
        }
    return {
        "str": str(exc),
        "repr": repr(exc),
        "dict": _sanitize_exception_dict(dict(getattr(exc, "__dict__", {}))),
        "status_code": getattr(exc, "status_code", None) or getattr(exc, "http_status_code", None),
        "response": http_response,
        "cause": _safe_exception_info(exc.__cause__) if getattr(exc, "__cause__", None) else None,
    }


_enhance_blob_error_logging()


class ImageValidationError(ValueError):
    pass


class StorageUnavailableError(RuntimeError):
    pass


def _normalized_result(
    output: io.BytesIO, extension: str, content_type: str
) -> tuple[bytes, str, str]:
    value = output.getvalue()
    if len(value) > config.MAX_UPLOAD_BYTES:
        raise ImageValidationError("normalized-image-size")
    return value, extension, content_type


def normalize_image(data: bytes) -> tuple[bytes, str, str]:
    if not data or len(data) > config.MAX_UPLOAD_BYTES:
        raise ImageValidationError("image-size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
            with Image.open(io.BytesIO(data)) as source:
                image_format = (source.format or "").upper()
                if image_format not in {"JPEG", "PNG", "WEBP"}:
                    raise ImageValidationError("image-format")
                source.seek(0)
                image = ImageOps.exif_transpose(source)
                image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                if image_format == "JPEG":
                    image.convert("RGB").save(
                        output,
                        format="JPEG",
                        quality=88,
                        optimize=True,
                        progressive=True,
                    )
                    return _normalized_result(output, ".jpg", "image/jpeg")
                if image_format == "PNG":
                    mode = "RGBA" if "A" in image.getbands() else "RGB"
                    image.convert(mode).save(output, format="PNG", optimize=True)
                    return _normalized_result(output, ".png", "image/png")
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                image.convert(mode).save(output, format="WEBP", quality=88, method=6)
                return _normalized_result(output, ".webp", "image/webp")
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageValidationError("invalid-image") from exc
    except Image.DecompressionBombWarning as exc:
        raise ImageValidationError("image-dimensions") from exc


async def save_public_image(data: bytes) -> str:
    normalized, extension, content_type = normalize_image(data)
    filename = f"{secrets.token_hex(16)}{extension}"
    if config.VERCEL or config.BLOB_READ_WRITE_TOKEN:
        if not config.BLOB_READ_WRITE_TOKEN:
            raise StorageUnavailableError("Vercel Blob is not configured")

        async with AsyncBlobClient(token=config.BLOB_READ_WRITE_TOKEN) as client:
            try:
                result = await client.put(
                    f"business/{filename}",
                    normalized,
                    access="private",
                    content_type=content_type,
                    add_random_suffix=False,
                    overwrite=False,
                    cache_control_max_age=31536000,
                )
            except BlobError as exc:
                exc_info = _safe_exception_info(exc)
                logger.error(
                    "Vercel BlobError upload failed path=%s content_type=%s size=%s token_present=%s exc_info=%s",
                    f"business/{filename}",
                    content_type,
                    len(normalized),
                    bool(config.BLOB_READ_WRITE_TOKEN),
                    json.dumps(exc_info, ensure_ascii=False),
                    exc_info=True,
                )
                raise
            except Exception:
                logger.exception(
                    "Vercel Blob upload failed path=%s content_type=%s size=%s token_present=%s",
                    f"business/{filename}",
                    content_type,
                    len(normalized),
                    bool(config.BLOB_READ_WRITE_TOKEN),
                )
                raise

        if not getattr(result, "url", None):
            raise StorageUnavailableError("Vercel Blob did not return a URL")
        return result.url

    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = config.UPLOAD_DIR / filename
    target.write_bytes(normalized)
    return f"/uploads/{filename}"
