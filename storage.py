import io
import logging
import secrets
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

import config
from vercel.blob import AsyncBlobClient

logger = logging.getLogger("booking")
Image.MAX_IMAGE_PIXELS = 20_000_000


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
            except Exception as exc:
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
