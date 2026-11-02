import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.sitemap import router as sitemap_router
from app.api.v1.router import api_router
from app.api.websocket import router as ws_router
from app.core.config import settings
from app.middleware.logging import RequestLoggingMiddleware
from app.middleware.rate_limit import limiter
from app.middleware.route_tracker import RouteTrackerMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def _health_warmer() -> None:
    """Keeps SMTP + Celery health caches warm so every request is a cache hit."""
    from app.services import health_service
    while True:
        await asyncio.sleep(8)  # initial warm happens in lifespan; this handles renewals
        try:
            await asyncio.gather(
                health_service.probe_smtp(),
                health_service.probe_celery(),
                return_exceptions=True,
            )
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting Pollard API...")
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    from app.db.init_db import init_db
    from app.services import health_service
    # Init DB and pre-warm health probes in parallel; cache is hot before first request.
    await asyncio.gather(
        init_db(),
        health_service.probe_smtp(),
        health_service.probe_celery(),
        return_exceptions=True,
    )
    warmer = asyncio.create_task(_health_warmer())
    logger.info("Pollard API started successfully")
    yield
    # Shutdown
    logger.info("Shutting down Pollard API...")
    warmer.cancel()
    from app.core.redis import close_redis
    await close_redis()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https://.*\.trycloudflare\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logging
app.add_middleware(RequestLoggingMiddleware)

# Per-route performance tracking (populates /admin/health/routes)
app.add_middleware(RouteTrackerMiddleware)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Routers
app.include_router(api_router)
app.include_router(ws_router)
app.include_router(sitemap_router)

# Static files (KYC uploads, etc.)
upload_dir = Path(settings.UPLOAD_DIR)
upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(upload_dir)), name="uploads")


# Exception handlers
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    errors = []
    for err in exc.errors():
        clean = {k: v for k, v in err.items() if k != "ctx"}
        if "ctx" in err:
            clean["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
        errors.append(clean)
    return JSONResponse(
        status_code=422,
        content={"detail": errors, "status_code": 422},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    # Capture to app_logs table in the background; never block the response
    try:
        from app.db.base import async_session_maker
        from app.services.app_log_service import capture_exception
        async with async_session_maker() as _db:
            await capture_exception(exc, _db, request=request, level="error")
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status_code": 500},
    )



# Health check
@app.get("/health")
async def health_check():
    return {"status": "ok", "version": settings.APP_VERSION}
