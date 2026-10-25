"""add live chat fields and encrypt support message text

Revision ID: e2f8b9a4d1c6
Revises: d8e3a6f1c4b7
Create Date: 2026-09-17

Adds channel ("email"|"chat") and assigned_agent_id to support_tickets for
the live-chat queue/claim workflow, and converts support_messages.text from
plaintext to AES-256-GCM ciphertext (same primitive/key as vote encryption,
see app/core/security.py) — existing rows are encrypted in place, not
dropped, so this is safe even if the table already has data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e2f8b9a4d1c6'
down_revision: Union[str, None] = 'd8e3a6f1c4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "support_tickets",
        sa.Column("channel", sa.String(10), nullable=False, server_default="email"),
    )
    op.add_column(
        "support_tickets",
        sa.Column(
            "assigned_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id"),
            nullable=True,
        ),
    )

    op.add_column("support_messages", sa.Column("text_encrypted", sa.LargeBinary(), nullable=True))

    from app.core.security import encrypt_text

    connection = op.get_bind()
    existing = connection.execute(sa.text("SELECT message_id, text FROM support_messages")).fetchall()
    for message_id, text in existing:
        connection.execute(
            sa.text("UPDATE support_messages SET text_encrypted = :enc WHERE message_id = :id"),
            {"enc": encrypt_text(text), "id": message_id},
        )

    op.alter_column("support_messages", "text_encrypted", nullable=False)
    op.drop_column("support_messages", "text")


def downgrade() -> None:
    op.add_column("support_messages", sa.Column("text", sa.Text(), nullable=True))

    from app.core.security import decrypt_text

    connection = op.get_bind()
    existing = connection.execute(sa.text("SELECT message_id, text_encrypted FROM support_messages")).fetchall()
    for message_id, text_encrypted in existing:
        connection.execute(
            sa.text("UPDATE support_messages SET text = :text WHERE message_id = :id"),
            {"text": decrypt_text(bytes(text_encrypted)), "id": message_id},
        )

    op.alter_column("support_messages", "text", nullable=False)
    op.drop_column("support_messages", "text_encrypted")

    op.drop_column("support_tickets", "assigned_agent_id")
    op.drop_column("support_tickets", "channel")
