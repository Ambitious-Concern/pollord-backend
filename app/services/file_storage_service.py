"""Client for the external file-storage service.

Files are uploaded to a central storage service via ``POST /upload``; the
service responds with a public ``downloadUrl`` that we persist in the database
(e.g. candidate ``image_url``, organization ``logo_url``/KYC documents).
"""

import base64
import binascii
import re

import httpx
from fastapi import HTTPException

from app.core.config import settings

# data:[<mediatype>][;base64],<data>
_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.DOTALL)

# Map common image mime types to file extensions for the uploaded filename.
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "application/pdf": "pdf",
}


def is_data_uri(value: str | None) -> bool:
    """True if the string is a base64/plain data URI (e.g. ``data:image/png;base64,...``)."""
    return bool(value) and _DATA_URI_RE.match(value) is not None


class FileStorageService:
    def __init__(self, base_url: str | None = None):
        # Strip trailing slash so we can safely append paths.
        self._base_url = (base_url or settings.FILE_STORAGE_URL).rstrip("/")

    async def upload_full(
        self,
        content: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> dict:
        """Upload raw bytes and return the storage service's full JSON body.

        The body contains at least ``fileId`` and ``downloadUrl``. Raises
        HTTPException(502) if the storage service is unreachable or returns an
        unexpected response.
        """
        files = {
            "file": (
                filename or "upload",
                content,
                content_type or "application/octet-stream",
            )
        }
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self._base_url}/upload",
                    files=files,
                    timeout=60.0,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"File storage service unreachable: {exc}",
            )

        if r.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail=f"File storage service error (HTTP {r.status_code})",
            )

        try:
            body = r.json()
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="File storage service returned an invalid response",
            )

        if not body.get("downloadUrl"):
            raise HTTPException(
                status_code=502,
                detail="File storage service did not return a download URL",
            )
        return body

    async def upload(
        self,
        content: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """Upload raw bytes and return the public download URL.

        Raises HTTPException(502) if the storage service is unreachable or
        returns an unexpected response.
        """
        body = await self.upload_full(content, filename, content_type)
        return body["downloadUrl"]

    async def upload_data_uri(self, data_uri: str, filename_hint: str = "upload") -> str:
        """Decode a base64 data URI, upload it, and return the download URL.

        Used to convert base64 payloads that arrive via JSON bodies into proper
        stored files so we never persist base64 blobs in the database.
        """
        match = _DATA_URI_RE.match(data_uri)
        if not match:
            raise HTTPException(status_code=400, detail="Invalid data URI")

        mime = (match.group("mime") or "application/octet-stream").strip()
        raw = match.group("data")

        if match.group("b64"):
            try:
                content = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(status_code=400, detail="Invalid base64 image data")
        else:
            content = raw.encode("utf-8")

        ext = _MIME_EXT.get(mime, "bin")
        return await self.upload(
            content=content,
            filename=f"{filename_hint}.{ext}",
            content_type=mime,
        )

    async def resolve(self, value: str | None, filename_hint: str = "upload") -> str | None:
        """Return an HTTP URL for ``value``.

        If ``value`` is a base64 data URI it is uploaded and replaced with its
        download URL; otherwise (already a URL, or ``None``) it is returned as-is.
        """
        if is_data_uri(value):
            return await self.upload_data_uri(value, filename_hint=filename_hint)
        return value


file_storage_service = FileStorageService()
