import json
from pathlib import Path
from typing import List

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App-wide configuration, loaded from environment variables / .env.
    Every field has a dev-safe default except secrets, which must be
    overridden in production (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # App
    APP_NAME: str = "Pollard API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    FRONTEND_URL: str = "http://localhost:5021"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://pollard:pollard@db:5432/pollard"
    # Password for the restricted, RLS-scoped Postgres role created by the
    # add-org-rls-policies migration. The app doesn't connect as this role
    # yet (see the RLS plan's deferred cutover) — required only when running
    # that migration, and read from the environment rather than ever being a
    # literal string in a committed migration file.
    RLS_APP_ROLE_PASSWORD: str = ""

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT
    SECRET_KEY: str = "change-me-in-production"
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Password
    BCRYPT_ROUNDS: int = 12

    # Vote Encryption
    AES_ENCRYPTION_KEY: str = "change-me-must-be-32-bytes-hex-encoded-key"
    HMAC_SECRET_KEY: str = "change-me-hmac-secret"

    # Email (Zoho SMTP)
    MAIL_HOST: str = "smtp.zoho.com"
    MAIL_PORT: int = 465
    MAIL_USERNAME: str = "noreply@theallex.com"
    MAIL_PASSWORD: str = ""
    MAIL_FROM: str = "noreply@theallex.com"
    MAIL_FROM_NAME: str = "Pollord"
    MAIL_USE_SSL: bool = True
    MAIL_USE_TLS: bool = False

    # OTP
    OTP_EXPIRE_MINUTES: int = 10

    # Live chat support — closed chats are purged (not kept indefinitely
    # like email tickets) this many days after closing.
    SUPPORT_CHAT_RETENTION_DAYS: int = 30

    # Paystack
    PAYSTACK_SECRET_KEY: str = "sk_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    PAYSTACK_PUBLIC_KEY: str = "pk_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    VOTE_PRICE: int = 100          # in smallest currency unit — 100 pesewas = ₵1 per vote
    VOTE_CURRENCY: str = "GHS"     # Ghana Cedis

    # JuniPay (USSD mobile money collections) — RS256 JWT signed with our
    # private key, verified by JuniPay against the public key on file with
    # them. No webhook signature scheme is documented, so the webhook
    # handler re-confirms via /checktranstatus rather than trusting the body.
    # PEM stored on one .env line with literal \n for newlines (a real
    # multi-line quoted value doesn't survive python-dotenv parsing here).
    JUNIPAY_CLIENT_ID: str = "change-me"
    JUNIPAY_PRIVATE_KEY: str = "change-me"

    # File uploads
    UPLOAD_DIR: str = "./uploads"
    # External file-storage service (uploads return a public download URL)
    FILE_STORAGE_URL: str = "http://62.171.191.76:7070/file-storage"
    # Where uploads actually go. Blank = auto: local disk while DEBUG, the
    # remote service otherwise — so a dev machine needs no storage service
    # running (and isn't writing into production's). Set explicitly to
    # "local"/"remote" to override.
    FILE_STORAGE_BACKEND: str = ""
    # Inside the container this sits on the bind-mounted source dir, so files
    # survive restarts and the Celery worker sees the same ones the API wrote.
    FILE_STORAGE_LOCAL_DIR: str = "/app/uploads"
    # Where transcoded image derivatives are cached. Safe to delete at any
    # time: a miss just re-encodes on the next request.
    IMAGE_CACHE_DIR: str = "/app/uploads/_derived"
    # Public HTTPS base URL of this API. Storage-service download URLs are
    # rewritten to point here (GET /api/v1/files/...) because the storage
    # service is plain HTTP and browsers block mixed content on HTTPS pages.
    PUBLIC_BASE_URL: str = "https://server.pollord.com"

    # CORS
    CORS_ORIGINS: str = '["http://localhost:5021","http://localhost:3001","http://localhost:8080"]'

    # WhatsApp (Meta Cloud API)
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "pollord-whatsapp-verify"
    WHATSAPP_APP_SECRET: str = ""       # Meta App Secret for webhook signature verification
    WHATSAPP_API_VERSION: str = "v19.0"

    # USSD (Arkesel gateway) + SMS confirmations
    # Arkesel's USSD callback has no signature scheme of its own, so the shared
    # secret below is required as a query param on the callback URL you
    # register with them (e.g. https://.../ussd/arkesel/callback?token=...) —
    # anyone without it gets rejected before we touch the DB.
    ARKESEL_API_KEY: str = ""
    ARKESEL_SMS_SENDER_ID: str = "Pollord"
    ARKESEL_WEBHOOK_TOKEN: str = ""
    USSD_SESSION_TTL_SECONDS: int = 180   # matches Arkesel's own USSD session timeout
    # Conservative single-screen budget — MTN Ghana's USSD gateway rejects/
    # truncates longer messages. Tune down further if a specific network
    # proves tighter than this in practice.
    USSD_MAX_MESSAGE_LENGTH: int = 160

    # Rate Limiting
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_VOTING: str = "10/minute"
    RATE_LIMIT_AUTH: str = "5/minute"

    # Short enough that "live results" still feels live, long enough to
    # absorb a refresh-spam burst on a close race without re-tallying every
    # vote on every request — see core/results_cache.py.
    LIVE_RESULTS_CACHE_TTL_SECONDS: int = 3

    @property
    def image_cache_dir(self) -> Path:
        return Path(self.IMAGE_CACHE_DIR)

    @property
    def use_local_file_storage(self) -> bool:
        backend = self.FILE_STORAGE_BACKEND.strip().lower()
        if backend in ("local", "remote"):
            return backend == "local"
        return self.DEBUG

    @property
    def cors_origins_list(self) -> List[str]:
        return json.loads(self.CORS_ORIGINS)

    # Secrets whose shipped defaults are public (they live in this file and in
    # .env.example), so leaving one in place silently voids whatever it
    # protects: AES guards votes and stored identity documents, HMAC guards
    # signed links, and the JWT/session keys guard every login.
    _REQUIRED_SECRETS = (
        "SECRET_KEY",
        "JWT_SECRET_KEY",
        "AES_ENCRYPTION_KEY",
        "HMAC_SECRET_KEY",
    )

    @model_validator(mode="after")
    def _refuse_default_secrets_outside_debug(self) -> "Settings":
        """Fail fast rather than booting production with placeholder secrets.

        DEBUG stays permissive so local/dev and the test suite keep working
        with the checked-in defaults.
        """
        if self.DEBUG:
            return self

        unset = [
            name
            for name in self._REQUIRED_SECRETS
            if str(getattr(self, name, "")).startswith("change-me")
        ]
        if unset:
            raise ValueError(
                "Refusing to start with default placeholder secrets: "
                + ", ".join(unset)
                + ". Set them in the environment, or set DEBUG=true for local use."
            )
        return self


settings = Settings()
