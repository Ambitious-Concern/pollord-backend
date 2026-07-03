import json
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # App
    APP_NAME: str = "Pollard API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    FRONTEND_URL: str = "http://localhost:5021"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://pollard:pollard@db:5432/pollard"

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

    # Paystack
    PAYSTACK_SECRET_KEY: str = "sk_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    PAYSTACK_PUBLIC_KEY: str = "pk_test_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    VOTE_PRICE: int = 100          # in smallest currency unit — 100 pesewas = ₵1 per vote
    VOTE_CURRENCY: str = "GHS"     # Ghana Cedis

    # File uploads
    UPLOAD_DIR: str = "./uploads"
    # External file-storage service (uploads return a public download URL)
    FILE_STORAGE_URL: str = "http://62.171.191.76:7070/file-storage"

    # CORS
    CORS_ORIGINS: str = '["http://localhost:5021","http://localhost:3001","http://localhost:8080"]'

    # WhatsApp (Meta Cloud API)
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "pollord-whatsapp-verify"
    WHATSAPP_APP_SECRET: str = ""       # Meta App Secret for webhook signature verification
    WHATSAPP_API_VERSION: str = "v19.0"

    # Rate Limiting
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_VOTING: str = "10/minute"
    RATE_LIMIT_AUTH: str = "5/minute"

    @property
    def cors_origins_list(self) -> List[str]:
        return json.loads(self.CORS_ORIGINS)


settings = Settings()
