from typing import Optional

from pydantic import BaseModel


class StartChatRequest(BaseModel):
    message: str


class ChatMessageRequest(BaseModel):
    text: str = ""
    attachment_url: Optional[str] = None
    attachment_name: Optional[str] = None
