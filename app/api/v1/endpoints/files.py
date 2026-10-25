"""Generic file-upload proxy.

Browsers cannot upload directly to the external file-storage service because it
does not allow their origin (CORS). This endpoint accepts a multipart upload
from the frontend (which our own CORS config permits) and forwards it to the
storage service server-to-server, returning the same ``fileId``/``downloadUrl``
shape the storage service returns.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.core.config import settings
from app.core.dependencies import get_current_active_user
from app.core.ownership import SYSTEM_ADMIN
from app.core.security import decrypt_file_bytes
from app.models.organization import Organization
from app.models.user import User
from app.services import image_service
from app.services.file_storage_service import file_storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["Files"])

# Generic uploads may be spreadsheets, documents, images, etc., so we do not
# restrict by type here (unlike the entity-specific image/KYC endpoints).
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
):
    """Upload a file and return ``{ fileId, downloadUrl }``.

    Forwards the file to the external storage service, sidestepping the browser
    CORS restriction that blocks uploading to that service directly.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="File must be under 100 MB")

    body = await file_storage_service.upload_full(
        content=content,
        filename=file.filename or "upload",
        content_type=file.content_type,
    )
    return body


@router.get("/{ext}/{file_id}")
async def download_file(
    request: Request,
    ext: str = Path(pattern=r"^[A-Za-z0-9]{1,10}$"),
    file_id: str = Path(pattern=r"^[A-Za-z0-9-]{1,64}$"),
    w: Optional[str] = Query(None, description="Target width; snapped to an allowlisted size"),
    q: Optional[str] = Query(None, description="Quality 40-95"),
):
    """Stream a stored file, transcoded and resized on demand for images.

    Public (no auth): these URLs are used directly in ``<img src>`` tags, which
    cannot send Authorization headers. The storage service is HTTP-only, so
    serving files through this HTTPS route avoids mixed-content blocking.

    Images are re-encoded to AVIF or WebP when the client explicitly says it
    accepts them, and cached on disk afterwards — AVIF costs most of a second
    to encode, so serving it uncached would be slower than the bytes saved.
    Anything non-image, or any format Pillow cannot read, is streamed through
    untouched.
    """
    content, content_type = await file_storage_service.download(ext, file_id)

    if not image_service.is_transcodable(ext):
        return _file_response(content, content_type, vary_accept=False)

    out_format = image_service.negotiate_format(request.headers.get("accept"), ext)
    width = image_service.normalise_width(w)
    quality = image_service.normalise_quality(q)

    try:
        content, content_type = await _variant(
            file_id, content, out_format, width, quality
        )
    except Exception:
        # A corrupt or exotic source shouldn't 500 an <img> tag — fall back to
        # whatever the storage service gave us.
        logger.warning(
            "Could not transcode %s/%s; serving original", ext, file_id, exc_info=True
        )

    return _file_response(content, content_type, vary_accept=True)


def _file_response(content: bytes, content_type: str, *, vary_accept: bool) -> Response:
    headers = {
        "Content-Disposition": "inline",
        # File IDs are unique per upload, so contents never change.
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    if vary_accept:
        # Without this a shared cache or CDN would hand an AVIF response to the
        # next client that cannot decode it — including link-preview crawlers.
        headers["Vary"] = "Accept"
    return Response(content=content, media_type=content_type, headers=headers)


async def _variant(
    file_id: str,
    content: bytes,
    out_format: str,
    width: Optional[int],
    quality: int,
) -> tuple[bytes, str]:
    """Return the requested derivative, generating and caching it on a miss."""
    cache_dir = settings.image_cache_dir
    key = image_service.cache_key(file_id, out_format, width, quality)
    path = image_service.cached_path(cache_dir, key, out_format)

    if path.is_file():
        return path.read_bytes(), image_service.mime_for(out_format)

    # Encoding is CPU-bound and AVIF is slow, so keep it off the event loop.
    data, mime = await run_in_threadpool(
        image_service.transcode, content, out_format, width, quality
    )

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a concurrent reader never sees a half-written file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        logger.warning("Could not cache derivative %s", path, exc_info=True)

    return data, mime


@router.get("/private/{ext}/{file_id}")
async def download_private_file(
    ext: str = Path(pattern=r"^[A-Za-z0-9]{1,10}$"),
    file_id: str = Path(pattern=r"^[A-Za-z0-9-]{1,64}$"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream a KYC identity document — organization owner or System Admin only.

    Unlike the sibling public route, identity documents are PII and must never
    be fetchable by URL alone, so this one authenticates the caller and checks
    they actually own the organization the document belongs to. Callers render
    it via an authenticated blob fetch rather than a bare <img src>.
    """
    ref = f"{ext}/{file_id}"
    result = await db.execute(
        select(Organization).where(
            or_(
                Organization.kyc_document_front.like(f"%{ref}"),
                Organization.kyc_document_back.like(f"%{ref}"),
            )
        )
    )
    org = result.scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="File not found")

    user_roles = [ur.role.role_name for ur in current_user.user_roles]
    if org.owner_id != current_user.user_id and SYSTEM_ADMIN not in user_roles:
        raise HTTPException(status_code=403, detail="You do not have access to this document")

    stored, stored_type = await file_storage_service.download(ext, file_id)

    try:
        content, content_type = decrypt_file_bytes(stored)
    except Exception:
        # Documents uploaded before private files were encrypted are still
        # plaintext in storage; serve them rather than 500ing, so the fix can
        # ship without a backfill. New uploads always take the branch above.
        content, content_type = stored, stored_type

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            # Explicitly uncacheable: a shared cache holding someone's ID
            # document would undo the authorization check above.
            "Cache-Control": "no-store, private",
        },
    )
