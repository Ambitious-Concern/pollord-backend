import secrets
from typing import List

import pyotp

ISSUER_NAME = "Pollord"


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def build_totp_uri(secret: str, email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER_NAME)


def verify_totp_code(secret: str, code: str) -> bool:
    """valid_window=1 tolerates one 30s step of clock drift either side."""
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_recovery_codes(count: int = 8) -> List[str]:
    return [f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}" for _ in range(count)]
