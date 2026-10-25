from pydantic import BaseModel


class StartChatRequest(BaseModel):
    message: str


class ChatMessageRequest(BaseModel):
    text: str
