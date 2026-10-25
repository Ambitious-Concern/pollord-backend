from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel

SearchEntityType = Literal["election", "event", "nominee", "ticket_type"]


class SearchResultItem(BaseModel):
    id: UUID
    type: SearchEntityType
    title: str
    subtitle: str
    status: Optional[str] = None


class SearchResponse(BaseModel):
    items: List[SearchResultItem]
