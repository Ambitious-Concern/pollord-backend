import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import decrypt_text, encrypt_text
from app.models.base import Base, TimestampMixin


class SupportTicket(TimestampMixin, Base):
    """24/7 (priority) email support — priority is derived at creation time
    from the creator's org's effective plan, see core/plans.py. Also backs
    live chat (channel="chat") — same ticket+message shape, just a faster
    cadence and a queue/claim workflow on top (see LiveChatService)."""

    __tablename__ = "support_tickets"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id"), nullable=True
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    # "email" (the original async ticket flow) or "chat" (live). Status
    # vocabularies differ by channel: email uses Open/Replied/Resolved,
    # chat uses Waiting/Active/Closed — same free-form column, no DB enum.
    channel: Mapped[str] = mapped_column(String(10), default="email", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="Open", nullable=False)
    priority: Mapped[bool] = mapped_column(Boolean, default=False)
    # Chat only — whichever agent claimed it out of the waiting queue.
    assigned_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )

    messages: Mapped[List["SupportMessage"]] = relationship(
        back_populates="ticket",
        order_by="SupportMessage.created_at",
        cascade="all, delete-orphan",
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.ticket_id"), nullable=False
    )
    sender_type: Mapped[str] = mapped_column(String(10), nullable=False)  # customer | agent
    sender_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    # AES-256-GCM (same key/primitive as vote encryption) — encrypted at
    # rest, never stored or logged as plaintext. Use the `text` property
    # below rather than this column directly.
    text_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped["SupportTicket"] = relationship(back_populates="messages")

    @property
    def text(self) -> str:
        return decrypt_text(self.text_encrypted)

    @text.setter
    def text(self, value: str) -> None:
        self.text_encrypted = encrypt_text(value)
