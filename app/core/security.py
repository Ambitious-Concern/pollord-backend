import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import bcrypt as _bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jose import JWTError, jwt

from app.core.config import settings


# Password hashing (using bcrypt directly for Python 3.13 compatibility)
def hash_password(password: str) -> str:
    salt = _bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return _bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


# JWT token management
def create_access_token(
    subject: str, expires_delta: Optional[timedelta] = None
) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode = {"sub": subject, "exp": expire, "type": "access"}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    to_encode = {"sub": subject, "exp": expire, "type": "refresh"}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except JWTError:
        return None


def create_email_verification_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    to_encode = {"sub": user_id, "exp": expire, "type": "email_verify"}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_password_reset_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=1)
    to_encode = {"sub": user_id, "exp": expire, "type": "password_reset"}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_candidate_result_token(
    candidate_id: str, election_id: str, election_end: Optional[datetime] = None
) -> str:
    """Token that expires 2 weeks after the election ends (or 2 weeks from now if end is unknown)."""
    base = election_end if election_end else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    expire = base + timedelta(weeks=2)
    to_encode = {
        "sub": candidate_id,
        "election_id": election_id,
        "type": "candidate_result",
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_ticket_download_token(ticket_id: str) -> str:
    """Long-lived token letting a guest (no account) re-download their ticket PDF."""
    expire = datetime.now(timezone.utc) + timedelta(days=365)
    to_encode = {"sub": ticket_id, "type": "ticket_download", "exp": expire}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_ticket_scan_token(event_id: str, expire: datetime) -> str:
    """Token letting anyone with the link check tickets in for exactly one
    event with no account, until `expire` (the caller decides the policy —
    e.g. end of the event's calendar day)."""
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    to_encode = {"sub": event_id, "type": "ticket_scan", "exp": expire}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


# Vote encryption (AES-256-GCM)
def _get_aes_key() -> bytes:
    key = settings.AES_ENCRYPTION_KEY
    if len(key) < 32:
        key = hashlib.sha256(key.encode()).digest()
    else:
        key = bytes.fromhex(key) if len(key) == 64 else key.encode()[:32]
    return key


def encrypt_vote(vote_data: dict) -> bytes:
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(vote_data).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def decrypt_vote(encrypted: bytes) -> dict:
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = encrypted[:12]
    ciphertext = encrypted[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode())


# Voter anonymization (HMAC-SHA256)
# Keyed by category_id (not election_id) so a voter can cast one free vote per
# category — the same person voting in two categories of the same election
# produces two different hashes instead of colliding on uq_vote_per_category.
def generate_voter_hash(user_id: UUID, category_id: UUID) -> str:
    message = f"{user_id}{category_id}"
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_anonymous_voter_hash(ip: str, user_agent: str, category_id: UUID) -> str:
    """Voter hash for unauthenticated public voters, keyed by IP + UA + category."""
    message = f"anon:{ip}:{user_agent}:{category_id}"
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_whatsapp_voter_hash(phone: str, category_id: UUID) -> str:
    """Voter hash for WhatsApp voters, keyed by phone number. Phone is never stored."""
    message = f"whatsapp:{phone}:{category_id}"
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_email_voter_hash(email: str, category_id: UUID) -> str:
    """Voter hash for OTP-verified public web voters, keyed by verified email.
    Stronger dedup than generate_anonymous_voter_hash's IP+UA (which is defeated
    by switching networks or clearing cookies) — email is never stored."""
    message = f"email:{email.lower()}:{category_id}"
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


# Vote signing
def sign_vote(vote_data: bytes, cast_at: str) -> str:
    message = vote_data + cast_at.encode()
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_vote_signature(vote_data: bytes, cast_at: str, signature: str) -> bool:
    expected = sign_vote(vote_data, cast_at)
    return hmac.compare_digest(expected, signature)


# Secure token generation
def generate_secure_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)
