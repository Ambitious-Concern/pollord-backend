import logging
import time

logger = logging.getLogger("pollard.access")


class RequestLoggingMiddleware:
    """Raw ASGI middleware — not BaseHTTPMiddleware, whose call_next() bridge
    buffers every response through an internal generator that's documented
    to break true streaming responses (SSE) and can hang on cancellation.
    Logs one line per request: client IP, method, path, status, duration."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        method = scope["method"]
        path = scope["path"]
        status_holder = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        duration = time.time() - start_time
        logger.info(
            f"{client_ip} - {method} {path} - {status_holder.get('status', '-')} - {duration:.3f}s"
        )
