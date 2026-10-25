"""On-demand image transcoding and resizing with a disk cache.

Derivatives are produced the first time a given (file, format, width, quality)
is requested and cached, so repeat hits are a plain file read. Encoding AVIF is
expensive (hundreds of ms), which is exactly why the result is kept.

Format selection is driven by the request's Accept header, and deliberately
only honours *explicit* support — see negotiate_format for why that matters to
link previews.
"""
import hashlib
import io
from pathlib import Path
from typing import Optional, Tuple

import pillow_avif  # noqa: F401  (registers the AVIF plugin with Pillow)
from PIL import Image

# Widths are allowlisted rather than free-form: an attacker could otherwise
# request ?w=1,2,3... and make us encode (and cache) thousands of variants,
# which is both a CPU and a disk exhaustion problem.
ALLOWED_WIDTHS = (64, 128, 256, 400, 640, 960, 1280, 1920)
QUALITY_RANGE = (40, 95)
DEFAULT_QUALITY = 78

# Pillow's own guard against decompression bombs; a 100MP "image" that expands
# to gigabytes of RGB would otherwise OOM the worker.
Image.MAX_IMAGE_PIXELS = 50_000_000

_TRANSCODABLE = {"jpg", "jpeg", "png", "webp", "avif"}

_PIL_FORMAT = {"avif": "AVIF", "webp": "WEBP", "jpeg": "JPEG", "png": "PNG"}
_MIME = {
    "avif": "image/avif",
    "webp": "image/webp",
    "jpeg": "image/jpeg",
    "png": "image/png",
}


def is_transcodable(ext: str) -> bool:
    return ext.lower() in _TRANSCODABLE


def negotiate_format(accept_header: Optional[str], source_ext: str) -> str:
    """Pick an output format from the Accept header.

    Only an explicit "image/avif" or "image/webp" counts. A client sending
    "*/*" — which is what most social crawlers do — gets the original format
    back, because Facebook, LinkedIn, WhatsApp and X do not reliably decode
    AVIF and several do not decode WebP either. Treating "*/*" as "supports
    everything" would silently break link previews, which is the one place
    these images most need to render.
    """
    accept = (accept_header or "").lower()
    if "image/avif" in accept:
        return "avif"
    if "image/webp" in accept:
        return "webp"
    return "png" if source_ext.lower() == "png" else "jpeg"


def normalise_width(raw: Optional[str]) -> Optional[int]:
    """Snap a requested width up to the nearest allowlisted size, or None."""
    if not raw:
        return None
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return None
    if requested <= 0:
        return None
    for width in ALLOWED_WIDTHS:
        if requested <= width:
            return width
    return ALLOWED_WIDTHS[-1]


def normalise_quality(raw: Optional[str]) -> int:
    try:
        quality = int(raw) if raw else DEFAULT_QUALITY
    except (TypeError, ValueError):
        return DEFAULT_QUALITY
    low, high = QUALITY_RANGE
    return max(low, min(high, quality))


def cache_key(file_id: str, out_format: str, width: Optional[int], quality: int) -> str:
    raw = f"{file_id}|{out_format}|{width or 'orig'}|{quality}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def mime_for(out_format: str) -> str:
    return _MIME.get(out_format, "application/octet-stream")


def transcode(
    content: bytes,
    out_format: str,
    width: Optional[int],
    quality: int,
) -> Tuple[bytes, str]:
    """Return (bytes, mime) for the requested variant.

    Raises on anything Pillow can't read; callers fall back to the original.
    """
    with Image.open(io.BytesIO(content)) as image:
        image.load()

        # AVIF/JPEG can't carry a palette or alpha-less RGBA sensibly; convert
        # up front so the encoder doesn't fail on exotic source modes.
        if out_format in ("jpeg",) and image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        elif image.mode == "P":
            image = image.convert("RGBA")

        if width and image.width > width:
            height = round(image.height * (width / image.width))
            image = image.resize((width, height), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        save_args = {"quality": quality}
        if out_format == "jpeg":
            # progressive renders sooner on slow connections; optimize costs a
            # little CPU once and is then cached forever.
            save_args.update(optimize=True, progressive=True)
        elif out_format == "png":
            save_args = {"optimize": True}

        image.save(buffer, _PIL_FORMAT[out_format], **save_args)
        return buffer.getvalue(), mime_for(out_format)


def cached_path(cache_dir: Path, key: str, out_format: str) -> Path:
    return cache_dir / f"{key}.{out_format}"
