from starlette.datastructures import MutableHeaders

from app.core.config import settings


class SecurityHeadersMiddleware:
    """Raw ASGI middleware — see logging.py's docstring for why this isn't
    a @app.middleware("http")/BaseHTTPMiddleware function; that style breaks
    streaming responses (SSE)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["X-XSS-Protection"] = "0"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                if not settings.DEBUG:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_wrapper)
