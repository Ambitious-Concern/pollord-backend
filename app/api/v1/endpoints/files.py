"""Generic file-upload proxy.

Browsers cannot upload directly to the external file-storage service because it
does not allow their origin (CORS). This endpoint accepts a multipart upload
from the frontend (which our own CORS config permits) and forwards it to the
storage service server-to-server, returning the same ``fileId``/``downloadUrl``
shape the storage service returns.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile
from fastapi.responses import Response

from app.core.dependencies import get_current_active_user
from app.models.user import User
from app.services.file_storage_service import file_storage_service

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
    ext: str = Path(pattern=r"^[A-Za-z0-9]{1,10}$"),
    file_id: str = Path(pattern=r"^[A-Za-z0-9-]{1,64}$"),
):
    """Stream a stored file from the external storage service.

    Public (no auth): these URLs are used directly in ``<img src>`` tags, which
    cannot send Authorization headers. The storage service is HTTP-only, so
    serving files through this HTTPS route avoids mixed-content blocking.
    """
    content, content_type = await file_storage_service.download(ext, file_id)
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": "inline",
            # File IDs are unique per upload, so contents never change.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
