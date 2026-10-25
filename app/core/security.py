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
    """Hash a plaintext password with a fresh bcrypt salt."""
    salt = _bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return _bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a plaintext password against a bcrypt hash from hash_password."""
    return _bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


# JWT token management
def create_access_token(
    subject: str,
    token_version: int = 0,
    expires_delta: Optional[timedelta] = None,
    session_id: Optional[str] = None,
) -> str:
    """Short-lived token for authenticating requests. token_version must
    match the user's current User.token_version (checked in
    dependencies.get_current_user) or it's rejected even if unexpired —
    that's what makes logout/password-change able to kill sessions.

    `session_id` (when given) is the paired refresh token's own jti — it
    lets a request identify which row in `refresh_tokens` it belongs to,
    which is how the Security settings page's session list marks "this
    device" without the frontend having to know its own refresh token.
    """
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode = {
        "sub": subject,
        "exp": expire,
        "type": "access",
        "ver": token_version,
        "jti": secrets.token_urlsafe(16),
        "sid": session_id,
    }
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str, token_version: int = 0) -> str:
    """Long-lived token used only to mint new access tokens. Single-use in
    practice — auth_service.refresh_token() revokes each one's jti (tracked
    in the refresh_tokens table) as soon as it's redeemed."""
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    to_encode = {
        "sub": subject,
        "exp": expire,
        "type": "refresh",
        "ver": token_version,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Verify signature + expiry and return the claims, or None if invalid
    for any reason (expired, wrong signature, malformed)."""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except JWTError:
        return None


def create_mfa_challenge_token(user_id: str) -> str:
    """Issued by login() once the password has already checked out for a
    2FA-enabled account — scoped only to completing that one login, not a
    general-purpose auth token (checked via its "type" claim)."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    to_encode = {"sub": user_id, "exp": expire, "type": "mfa_challenge"}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_email_verification_token(user_id: str) -> str:
    """Link-based email verification token, valid 24h."""
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    to_encode = {"sub": user_id, "exp": expire, "type": "email_verify"}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_password_reset_token(user_id: str) -> str:
    """Password reset link token, valid 1h."""
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
    """Normalize AES_ENCRYPTION_KEY to 32 bytes regardless of whether it was
    configured as a raw string, a 64-char hex string, or something shorter."""
    key = settings.AES_ENCRYPTION_KEY
    if len(key) < 32:
        key = hashlib.sha256(key.encode()).digest()
    else:
        key = bytes.fromhex(key) if len(key) == 64 else key.encode()[:32]
    return key


def encrypt_vote(vote_data: dict) -> bytes:
    """Encrypt a vote payload; output is nonce || ciphertext, ready to store."""
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(vote_data).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext


# Private-file encryption (AES-256-GCM), same key as votes.
#
# The external file-storage service is plain HTTP with no authentication, so
# anything uploaded there is readable by anyone who learns its file id. Identity
# documents therefore go up already encrypted and are only decrypted when served
# through the authenticated /files/private route — a leaked object is ciphertext.
#
# Layout: nonce(12) || AESGCM(ct_len(2) || content_type || file_bytes), so the
# original content type survives the round trip without a second column or a
# base64 envelope inflating every image by a third.
def encrypt_file_bytes(content: bytes, content_type: str | None) -> bytes:
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ct = (content_type or "application/octet-stream").encode()
    if len(ct) > 0xFFFF:
        ct = ct[:0xFFFF]
    plaintext = len(ct).to_bytes(2, "big") + ct + content
    return nonce + aesgcm.encrypt(nonce, plaintext, None)


def decrypt_file_bytes(blob: bytes) -> tuple[bytes, str]:
    """Inverse of encrypt_file_bytes, returning (content, content_type)."""
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(blob[:12], blob[12:], None)
    ct_len = int.from_bytes(plaintext[:2], "big")
    content_type = plaintext[2 : 2 + ct_len].decode(errors="replace")
    return plaintext[2 + ct_len :], content_type


def decrypt_vote(encrypted: bytes) -> dict:
    """Inverse of encrypt_vote."""
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = encrypted[:12]
    ciphertext = encrypted[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode())


# Support chat message encryption — same AES-256-GCM primitive/key as vote
# encryption above, just wrapping plain text instead of a vote payload.
def encrypt_text(text: str) -> bytes:
    """Encrypt a text payload; output is nonce || ciphertext, ready to store."""
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ciphertext = aesgcm.encrypt(nonce, text.encode(), None)
    return nonce + ciphertext


def decrypt_text(encrypted: bytes) -> str:
    """Inverse of encrypt_text."""
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = encrypted[:12]
    ciphertext = encrypted[12:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


# Voter anonymization (HMAC-SHA256)
# Keyed by category_id (not election_id) so a voter can cast one free vote per
# category — the same person voting in two categories of the same election
# produces two different hashes instead of colliding on uq_vote_per_category.
def generate_voter_hash(user_id: UUID, category_id: UUID) -> str:
    """Voter hash for authenticated (logged-in) voters."""
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


def generate_sms_voter_hash(phone: str, category_id: UUID) -> str:
    """Voter hash for SMS-OTP-verified public web voters, keyed by the
    verified phone number. Premium-only alternative to
    generate_email_voter_hash — phone is never stored."""
    message = f"sms:{phone}:{category_id}"
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_ussd_voter_hash(phone: str, category_id: UUID) -> str:
    """Voter hash for USSD voters, keyed by the phone number that dialed in.
    Phone is never stored — same pattern as generate_whatsapp_voter_hash."""
    message = f"ussd:{phone}:{category_id}"
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


# Vote signing
def sign_vote(vote_data: bytes, cast_at: str) -> str:
    """HMAC over the encrypted vote + cast timestamp, so tampering with
    either the stored ciphertext or its recorded time is detectable."""
    message = vote_data + cast_at.encode()
    return hmac.new(
        settings.HMAC_SECRET_KEY.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_vote_signature(vote_data: bytes, cast_at: str, signature: str) -> bool:
    """Constant-time check of a signature produced by sign_vote."""
    expected = sign_vote(vote_data, cast_at)
    return hmac.compare_digest(expected, signature)


def sign_data(data: bytes) -> str:
    """Generic HMAC over arbitrary bytes — for signed audit certificates and
    anything else that isn't specifically a cast vote."""
    return hmac.new(settings.HMAC_SECRET_KEY.encode(), data, hashlib.sha256).hexdigest()


def verify_data_signature(data: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_data(data), signature)


# Secure token generation
def generate_secure_token(length: int = 32) -> str:
    """General-purpose random token (e.g. one-off secrets, IDs)."""
    return secrets.token_urlsafe(length)
