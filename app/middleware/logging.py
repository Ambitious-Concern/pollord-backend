import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("pollard.access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()
        client_ip = request.client.host if request.client else "unknown"
        method = request.method
        path = request.url.path

        response = await call_next(request)

        duration = time.time() - start_time
        logger.info(
            f"{client_ip} - {method} {path} - {response.status_code} - {duration:.3f}s"
        )

        return response
