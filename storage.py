import io
import logging
import secrets
import warnings
from urllib.parse import quote

from PIL import Image, ImageOps, UnidentifiedImageError

import config

logger = logging.getLogger("storage")
Image.MAX_IMAGE_PIXELS = 20_000_000

_BUCKET = "business-images"
_supabase_client = None


class ImageValidationError(ValueError):
    pass


class StorageUnavailableError(RuntimeError):
    pass


def _get_supabase_client():
    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        raise StorageUnavailableError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY are not configured"
        )

    try:
        from supabase import create_client
    except Exception as exc:
        raise StorageUnavailableError("Supabase client is not installed") from exc

    try:
        _supabase_client = create_client(
            config.SUPABASE_URL,
            config.SUPABASE_SERVICE_KEY,
        )
    except Exception as exc:
        raise StorageUnavailableError("Could not create Supabase client") from exc

    return _supabase_client


def _normalized_result(
    output: io.BytesIO,
    extension: str,
    content_type: str,
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
                image.convert(mode).save(
                    output,
                    format="WEBP",
                    quality=88,
                    method=6,
                )
                return _normalized_result(output, ".webp", "image/webp")

    except ImageValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise ImageValidationError("invalid-image") from exc
    except Image.DecompressionBombWarning as exc:
        raise ImageValidationError("image-dimensions") from exc


def _bucket_name(item) -> str | None:
    if isinstance(item, dict):
        return item.get("name") or item.get("id")
    return getattr(item, "name", None) or getattr(item, "id", None)


def _ensure_public_bucket(client) -> None:
    try:
        buckets = client.storage.list_buckets()
        names = {_bucket_name(item) for item in buckets}
    except Exception as exc:
        raise StorageUnavailableError(
            f"Could not list Supabase buckets: {exc}"
        ) from exc

    if _BUCKET in names:
        return

    try:
        # Supabase client versions use slightly different signatures.
        try:
            client.storage.create_bucket(
                _BUCKET,
                options={
                    "public": True,
                    "file_size_limit": config.MAX_UPLOAD_BYTES,
                    "allowed_mime_types": [
                        "image/jpeg",
                        "image/png",
                        "image/webp",
                    ],
                },
            )
        except TypeError:
            client.storage.create_bucket(
                _BUCKET,
                name=_BUCKET,
                options={
                    "public": True,
                    "file_size_limit": config.MAX_UPLOAD_BYTES,
                    "allowed_mime_types": [
                        "image/jpeg",
                        "image/png",
                        "image/webp",
                    ],
                },
            )
    except Exception as exc:
        raise StorageUnavailableError(
            f"Could not create public Supabase bucket '{_BUCKET}': {exc}"
        ) from exc


async def save_public_image(data: bytes) -> str:
    normalized, extension, content_type = normalize_image(data)
    filename = f"{secrets.token_hex(16)}{extension}"
    object_path = f"business/{filename}"

    client = _get_supabase_client()
    _ensure_public_bucket(client)

    try:
        bucket = client.storage.from_(_BUCKET)
        bucket.upload(
            object_path,
            normalized,
            {
                "content-type": content_type,
                "upsert": "false",
            },
        )
    except Exception as exc:
        logger.exception(
            "Supabase upload failed bucket=%s path=%s",
            _BUCKET,
            object_path,
        )
        raise StorageUnavailableError(f"Supabase upload failed: {exc}") from exc

    # Build the public URL directly so the return type is always a plain string.
    base = config.SUPABASE_URL.rstrip("/")
    encoded_path = quote(object_path, safe="/")
    return f"{base}/storage/v1/object/public/{_BUCKET}/{encoded_path}"


def delete_image_by_url(url: str) -> bool:
    if not url:
        return True

    marker = f"/storage/v1/object/public/{_BUCKET}/"
    if marker not in url:
        return True

    object_path = url.split(marker, 1)[1]
    if not object_path:
        return True

    try:
        client = _get_supabase_client()
        client.storage.from_(_BUCKET).remove([object_path])
        return True
    except Exception:
        logger.exception(
            "Supabase delete failed bucket=%s path=%s",
            _BUCKET,
            object_path,
        )
        return False