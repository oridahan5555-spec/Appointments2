import io
import secrets
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

import config
from typing import Optional

# Preserve AsyncBlobClient symbol for tests/compatibility (may be monkeypatched)
try:
    from vercel.blob import AsyncBlobClient  # type: ignore
except Exception:
    AsyncBlobClient = None

# Lazy import for supabase client; only required when SUPABASE_URL is configured.
_supabase_client = None
def _get_supabase_client():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return None
    try:
        from supabase import create_client
    except Exception:
        return None
    _supabase_client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    return _supabase_client

Image.MAX_IMAGE_PIXELS = 20_000_000


class ImageValidationError(ValueError):
    pass


class StorageUnavailableError(RuntimeError):
    pass


def _normalized_result(output: io.BytesIO, extension: str, content_type: str) -> tuple[bytes, str, str]:
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
    # If Vercel AsyncBlobClient is available and a blob token or Vercel runtime is set, prefer it
    if (config.VERCEL or config.BLOB_READ_WRITE_TOKEN) and AsyncBlobClient is not None:
        async with AsyncBlobClient() as client:
            result = await client.put(
                f"business/{filename}",
                normalized,
                access="private",
                content_type=content_type,
                add_random_suffix=False,
                overwrite=False,
                cache_control_max_age=31536000,
            )

        if not getattr(result, "url", None):
            raise StorageUnavailableError("Vercel Blob did not return a URL")
        return result.url

    # If Supabase is configured, upload there
    supabase = _get_supabase_client()
    bucket = "business-images"
    if supabase is not None:
        # ensure bucket exists (best-effort)
        try:
            buckets = supabase.storage.list_buckets()
            names = [b["name"] if isinstance(b, dict) and "name" in b else getattr(b, "name", None) for b in buckets]
            if bucket not in names:
                try:
                    supabase.storage.create_bucket(bucket, public=False)
                except Exception:
                    # ignore create failures (may already exist or not permitted)
                    pass
        except Exception:
            # ignore listing errors
            pass

        path = f"business/{filename}"
        try:
            res = supabase.storage.from_(bucket).upload(path, normalized, content_type=content_type)
        except Exception as exc:
            raise StorageUnavailableError(str(exc)) from exc

        # Construct public URL (signed URL would require service key)
        try:
            public = supabase.storage.from_(bucket).get_public_url(path)
            url = public.get("publicURL") or public.get("public_url") or public
        except Exception:
            # fallback: build relative path
            url = f"/uploads/{filename}"
        return url

    # Local filesystem fallback for non-Supabase runs
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = config.UPLOAD_DIR / filename
    target.write_bytes(normalized)
    return f"/uploads/{filename}"


def delete_image_by_url(url: str) -> bool:
    """Delete an image given a URL or local path. Returns True if deleted or not applicable."""
    if not url:
        return True
    # Local file
    if url.startswith("/uploads/"):
        target = config.UPLOAD_DIR / url.split("/uploads/", 1)[1]
        try:
            if target.is_file():
                target.unlink()
            return True
        except Exception:
            return False

    # Supabase URL
    supabase = _get_supabase_client()
    if supabase is None:
        return False
    # Attempt to derive bucket and path
    try:
        parsed = url.split('/')
        # Supabase public url format contains /storage/v1/object/public/<bucket>/<path>
        if 'storage' in parsed and 'object' in parsed and 'public' in parsed:
            # find index of 'public'
            idx = parsed.index('public')
            bucket = parsed[idx+1]
            path = '/'.join(parsed[idx+2:])
        else:
            # fallback to business/<filename>
            bucket = 'business-images'
            path = url.split(bucket + '/')[-1]
        res = supabase.storage.from_(bucket).remove([path])
        return True
    except Exception:
        return False
