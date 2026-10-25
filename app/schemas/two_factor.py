from typing import List

from pydantic import BaseModel, field_validator


class TwoFactorSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TwoFactorEnableRequest(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 6:
            raise ValueError("Code must be 6 digits")
        return v


class TwoFactorCodesResponse(BaseModel):
    recovery_codes: List[str]


class TwoFactorDisableRequest(BaseModel):
    password: str


class RegenerateRecoveryCodesRequest(BaseModel):
    password: str
