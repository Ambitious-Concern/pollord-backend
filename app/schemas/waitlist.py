from pydantic import BaseModel, EmailStr


class WaitlistSubscribeRequest(BaseModel):
    email: EmailStr


class WaitlistSubscribeResponse(BaseModel):
    message: str


class WaitlistAnnounceResponse(BaseModel):
    sent: int
    skipped: int
