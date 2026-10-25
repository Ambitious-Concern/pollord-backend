"""Client for the external file-storage service.

Files are uploaded to a central storage service via ``POST /upload``; the
service responds with a public ``downloadUrl`` that we persist in the database
(e.g. candidate ``image_url``, organization ``logo_url``/KYC documents).
"""

import base64
import binascii
import logging
import mimetypes
import re
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.core.config import settings
from app.core.security import encrypt_file_bytes

logger = logging.getLogger(__name__)

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


# Python's mimetypes doesn't know the OOXML formats, so an exported .xlsx
# would otherwise be served as application/octet-stream and a browser would
# treat a perfectly good workbook as an unknown binary.
_EXTRA_MIME_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class FileStorageService:
    """Uploads/downloads files to the external file-storage service and
    rewrites its URLs to our HTTPS proxy route (see to_public_url)."""

    def __init__(self, base_url: str | None = None):
        # Strip trailing slash so we can safely append paths.
        self._base_url = (base_url or settings.FILE_STORAGE_URL).rstrip("/")

    @property
    def _local_dir(self) -> Path | None:
        """The directory to read/write when running on local disk, else None."""
        if not settings.use_local_file_storage:
            return None
        return Path(settings.FILE_STORAGE_LOCAL_DIR)

    def _local_path(self, root: Path, ext: str, file_id: str) -> Path:
        """Resolve ext/file_id under root, refusing anything that escapes it.

        The HTTP routes already constrain both segments by regex, but this is
        also reachable from internal callers, and a path traversal here would
        read arbitrary files off the server.
        """
        candidate = (root / ext / file_id).resolve()
        if not str(candidate).startswith(str(root.resolve())):
            raise HTTPException(status_code=400, detail="Invalid file path")
        return candidate

    def _local_upload(self, content: bytes, filename: str) -> dict:
        root = self._local_dir
        assert root is not None
        ext = "".join(c for c in Path(filename or "").suffix.lstrip(".") if c.isalnum())[:10].lower()
        ext = ext or "bin"
        file_id = uuid.uuid4().hex

        dest = self._local_path(root, ext, file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

        # Same URL shape the remote service returns, so to_public_url /
        # to_private_url and the /files routes behave identically either way.
        return {
            "fileId": file_id,
            "downloadUrl": self.to_public_url(f"{self._base_url}/{ext}/{file_id}"),
        }

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
        if self._local_dir is not None:
            return self._local_upload(content, filename)

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
        body["downloadUrl"] = self.to_public_url(body["downloadUrl"])
        return body

    def to_public_url(self, download_url: str) -> str:
        """Rewrite a storage-service download URL to our HTTPS proxy route.

        The storage service is reachable only over plain HTTP, which browsers
        block as mixed content on HTTPS pages, so clients must fetch files
        through GET /api/v1/files/{ext}/{file_id} instead.
        """
        if download_url.startswith(f"{self._base_url}/"):
            path = download_url[len(self._base_url):]
            return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/files{path}"
        return download_url

    def to_private_url(self, download_url: str) -> str:
        """Rewrite a storage-service URL to our authenticated proxy route.

        Used for identity documents: the resulting URL is only servable to the
        organization owner or a System Administrator, unlike to_public_url's
        route which deliberately serves anyone so <img src> works.
        """
        if download_url.startswith(f"{self._base_url}/"):
            path = download_url[len(self._base_url):]
            return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/files/private{path}"
        return download_url

    async def upload_private(
        self,
        *,
        content: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        """Encrypt, upload, and return an authenticated-only download URL.

        Encryption happens here rather than being left to the storage service
        because that service has no authentication: anything readable by file
        id is readable by the world, so identity documents must arrive there
        already unreadable.
        """
        body = await self.upload_full(
            content=encrypt_file_bytes(content, content_type),
            filename=filename,
            content_type="application/octet-stream",
        )
        # upload_full already rewrote downloadUrl to the public route; swap that
        # prefix for the private one so the document is never served unauthenticated.
        public_prefix = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/files"
        url = body["downloadUrl"]
        if url.startswith(f"{public_prefix}/"):
            return f"{public_prefix}/private{url[len(public_prefix):]}"
        return url

    async def download(self, ext: str, file_id: str) -> tuple[bytes, str]:
        """Fetch a stored file, returning ``(content, content_type)``.

        Raises HTTPException(404) if the file does not exist and
        HTTPException(502) if the storage service is unreachable.
        """
        root = self._local_dir
        if root is not None:
            path = self._local_path(root, ext, file_id)
            if not path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            guessed, _ = mimetypes.guess_type(f"placeholder.{ext}")
            content_type = _EXTRA_MIME_TYPES.get(ext.lower()) or guessed
            return path.read_bytes(), content_type or "application/octet-stream"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self._base_url}/{ext}/{file_id}",
                    timeout=60.0,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"File storage service unreachable: {exc}",
            )
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="File not found")
        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"File storage service error (HTTP {r.status_code})",
            )
        return r.content, r.headers.get("content-type", "application/octet-stream")

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

    def extract_path_parts(self, public_url: str) -> tuple[str, str] | None:
        """Extract ``(ext, file_id)`` from a public proxy URL, or None if not recognised."""
        prefix = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/api/v1/files/"
        if not public_url.startswith(prefix):
            return None
        rest = public_url[len(prefix):]
        parts = rest.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        return parts[0], parts[1]

    async def delete(self, ext: str, file_id: str) -> None:
        """Delete a stored file. Logs but does not raise on remote errors."""
        root = self._local_dir
        if root is not None:
            path = self._local_path(root, ext, file_id)
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                logger.warning("Could not delete local file %s/%s", ext, file_id, exc_info=True)
            return

        try:
            async with httpx.AsyncClient() as client:
                await client.delete(f"{self._base_url}/{ext}/{file_id}", timeout=15.0)
        except Exception:
            logger.warning("Could not delete %s/%s from storage service", ext, file_id, exc_info=True)

    async def resolve(self, value: str | None, filename_hint: str = "upload") -> str | None:
        """Return an HTTP URL for ``value``.

        If ``value`` is a base64 data URI it is uploaded and replaced with its
        download URL; otherwise (already a URL, or ``None``) it is returned as-is.
        """
        if is_data_uri(value):
            return await self.upload_data_uri(value, filename_hint=filename_hint)
        return value


file_storage_service = FileStorageService()
