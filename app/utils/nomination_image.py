"""Security validation and sanitisation for nomination photo uploads.

Three-layer defence:
  1. MIME allowlist — only image/jpeg, image/png, image/webp.
  2. Magic-byte check — first bytes must match a known image signature; a
     renamed executable with a .jpg extension is caught here before Pillow opens it.
  3. Pillow re-encode — opens the image and re-saves it to the same format.
     This strips all EXIF/metadata and catches polyglot files (e.g. a valid JPEG
     header followed by a ZIP payload) that Pillow truncates on open.
"""
import io

from fastapi import HTTPException
from PIL import Image

MAX_BYTES = 5 * 1024 * 1024  # 5 MB

ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

_MAGIC: dict[str, list[bytes]] = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png":  [b"\x89PNG\r\n\x1a\n"],
    "image/webp": [b"RIFF"],  # bytes 8-11 must also spell "WEBP"
}

_PIL_FORMAT: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/png":  "PNG",
    "image/webp": "WEBP",
}

CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png":  "png",
    "image/webp": "webp",
}


def _check_magic(content: bytes, content_type: str) -> None:
    for sig in _MAGIC.get(content_type, []):
        if content[: len(sig)] == sig:
            if content_type == "image/webp" and content[8:12] != b"WEBP":
                raise HTTPException(status_code=400, detail="Invalid image file")
            return
    raise HTTPException(status_code=400, detail="Invalid image file")


def validate_and_sanitise(content: bytes, content_type: str | None) -> tuple[bytes, str]:
    """Validate and re-encode an image, returning (clean_bytes, content_type).

    Raises HTTPException 400 on any validation failure.
    """
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Only JPEG, PNG, and WebP images are accepted",
        )
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image must be under 5 MB")

    _check_magic(content, ct)

    try:
        # verify() catches truncated files but closes the handle, so re-open afterwards.
        Image.open(io.BytesIO(content)).verify()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or corrupt image file")

    try:
        img = Image.open(io.BytesIO(content))
        pil_fmt = _PIL_FORMAT[ct]

        # JPEG and WebP cannot store an alpha channel — flatten onto white background.
        if pil_fmt in ("JPEG", "WEBP") and img.mode not in ("RGB", "L"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        elif img.mode == "P":
            # Palette mode needs conversion before saving.
            img = img.convert("RGBA" if pil_fmt == "PNG" else "RGB")

        buf = io.BytesIO()
        kwargs: dict = {"format": pil_fmt}
        if pil_fmt == "JPEG":
            kwargs.update(quality=90, optimize=True)
        elif pil_fmt == "WEBP":
            kwargs["quality"] = 90
        img.save(buf, **kwargs)
        return buf.getvalue(), ct
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or corrupt image file")
