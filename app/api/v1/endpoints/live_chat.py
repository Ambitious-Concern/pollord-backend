"""Live chat support: customer-side start/message/stream, agent-side
queue/claim/reply/close. Shares SupportTicket/SupportMessage with the async
email flow (support.py) — see LiveChatService for the actual logic.

Route order matters here: /queue/* must be registered before any
/{ticket_id}/* route, or FastAPI would match "queue" as a ticket_id for any
path shaped like {ticket_id}/stream (both /queue/stream and
/{ticket_id}/stream have the same template)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.core.support_chat_stream import stream_queue, stream_ticket
from app.db.base import async_session_maker, get_db
from app.models.user import User
from app.schemas.live_chat import ChatMessageRequest, StartChatRequest
from app.schemas.support import SupportTicketResponse
from app.services.live_chat_service import LiveChatService

router = APIRouter(prefix="/support/chat", tags=["Live Chat"])

AGENT_ROLES = ("System Administrator",)
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# --- Agent side: queue (registered first, see module docstring) ----------


@router.get("/queue/list", response_model=list[SupportTicketResponse])
async def list_waiting_chats(
    current_user: User = Depends(require_roles(*AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).list_waiting()


async def _compute_queue_state() -> list:
    async with async_session_maker() as db:
        chats = await LiveChatService(db).list_waiting()
        return [SupportTicketResponse.model_validate(c) for c in chats]


@router.get("/queue/stream")
async def stream_queue_endpoint(
    current_user: User = Depends(require_roles(*AGENT_ROLES)),
):
    generator = stream_queue(_compute_queue_state)
    return StreamingResponse(generator, media_type="text/event-stream", headers=_SSE_HEADERS)


# --- Customer side ---------------------------------------------------------


@router.post("/start", response_model=SupportTicketResponse, status_code=201)
async def start_chat(
    payload: StartChatRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).start_chat(current_user.user_id, payload.message)


@router.get("/{ticket_id}", response_model=SupportTicketResponse)
async def get_chat(
    ticket_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).get_chat_for_customer(ticket_id, current_user.user_id)


@router.post("/{ticket_id}/messages", response_model=SupportTicketResponse)
async def send_message(
    ticket_id: UUID,
    payload: ChatMessageRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).send_customer_message(ticket_id, current_user.user_id, payload.text)


async def _compute_ticket_state(ticket_id: UUID) -> SupportTicketResponse:
    # A fresh, short-lived session per recompute — an SSE connection can stay
    # open far longer than a normal request (see stream_public_live_results).
    async with async_session_maker() as db:
        ticket = await LiveChatService(db).get_for_stream(ticket_id)
        return SupportTicketResponse.model_validate(ticket)


@router.get("/{ticket_id}/stream")
async def stream_chat(
    ticket_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE stream for one conversation — either the customer or the
    assigned agent may open it."""
    ticket = await LiveChatService(db).get_for_stream(ticket_id)
    is_agent = any(ur.role.role_name in AGENT_ROLES for ur in current_user.user_roles)
    if ticket.user_id != current_user.user_id and not is_agent:
        raise HTTPException(status_code=403, detail="You do not have access to this chat")

    generator = stream_ticket(ticket_id, SupportTicketResponse, lambda: _compute_ticket_state(ticket_id))
    return StreamingResponse(generator, media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/{ticket_id}/claim", response_model=SupportTicketResponse)
async def claim_chat(
    ticket_id: UUID,
    current_user: User = Depends(require_roles(*AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).claim(ticket_id, current_user.user_id)


@router.post("/{ticket_id}/agent-messages", response_model=SupportTicketResponse)
async def send_agent_message(
    ticket_id: UUID,
    payload: ChatMessageRequest,
    current_user: User = Depends(require_roles(*AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).send_agent_message(ticket_id, current_user.user_id, payload.text)


@router.post("/{ticket_id}/close", response_model=SupportTicketResponse)
async def close_chat(
    ticket_id: UUID,
    current_user: User = Depends(require_roles(*AGENT_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await LiveChatService(db).close(ticket_id)
