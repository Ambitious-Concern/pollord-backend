"""Email support tickets + live chats across every status the dashboard
filters on (Open/Replied/Resolved for email; Waiting/Active/Closed for
chat) — messages go through SupportMessage's normal text property, so
they're encrypted at rest exactly like real ones."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support_ticket import SupportMessage, SupportTicket
from app.models.user import User
from seeders import config
from seeders.config import rng

_CATEGORIES = [
    "Billing & Payouts", "Technical issue", "Voting & Elections",
    "Events & Ticketing", "Account & Verification", "Other",
]
_EMAIL_STATUSES = ["Open", "Replied", "Resolved"]
_SAMPLE_MESSAGES = [
    "My payout hasn't landed yet, can you check?",
    "I can't verify my organization's KYC documents.",
    "A voter says their receipt code isn't working.",
    "How do I add a teammate to my organization?",
    "The ticket scanner says invalid but the QR looks fine.",
    "Can I get a refund for a duplicate ticket purchase?",
]


async def seed_support_tickets(db: AsyncSession, voters: list[User]) -> None:
    email_count = (
        await db.execute(select(func.count()).select_from(SupportTicket).where(SupportTicket.channel == "email"))
    ).scalar_one()

    now = datetime.now(timezone.utc)
    for i in range(email_count, config.N_SUPPORT_EMAIL_TICKETS):
        voter = rng.choice(voters)
        ticket = SupportTicket(
            user_id=voter.user_id,
            subject=rng.choice(_SAMPLE_MESSAGES)[:60],
            category=rng.choice(_CATEGORIES),
            channel="email",
            status=rng.choice(_EMAIL_STATUSES),
            priority=rng.random() < 0.2,
        )
        db.add(ticket)
        await db.flush()
        db.add(SupportMessage(
            ticket_id=ticket.ticket_id, sender_type="customer", sender_user_id=voter.user_id,
            text=rng.choice(_SAMPLE_MESSAGES), created_at=now - timedelta(days=rng.randint(0, 45)),
        ))
        if ticket.status != "Open":
            db.add(SupportMessage(
                ticket_id=ticket.ticket_id, sender_type="agent", sender_user_id=voter.user_id,
                text="Thanks for reaching out — we're looking into this now.",
                created_at=now - timedelta(days=rng.randint(0, 30)),
            ))

    await db.flush()


async def seed_live_chats(db: AsyncSession, voters: list[User], agents: list[User]) -> None:
    chat_count = (
        await db.execute(select(func.count()).select_from(SupportTicket).where(SupportTicket.channel == "chat"))
    ).scalar_one()

    now = datetime.now(timezone.utc)
    seen_names: set = set()
    for i in range(chat_count, config.N_SUPPORT_CHATS):
        voter = rng.choice(voters)
        state = rng.choice(["waiting", "waiting", "active", "closed", "closed", "closed"])
        status = {"waiting": "Waiting", "active": "Active", "closed": "Closed"}[state]
        agent = rng.choice(agents) if state != "waiting" else None

        chat = SupportTicket(
            user_id=voter.user_id,
            subject="Live chat",
            category="Live Chat",
            channel="chat",
            status=status,
            assigned_agent_id=agent.user_id if agent else None,
        )
        db.add(chat)
        await db.flush()
        db.add(SupportMessage(
            ticket_id=chat.ticket_id, sender_type="customer", sender_user_id=voter.user_id,
            text="Hi, I need help with something urgent.", created_at=now - timedelta(hours=rng.randint(1, 200)),
        ))
        if agent:
            db.add(SupportMessage(
                ticket_id=chat.ticket_id, sender_type="agent", sender_user_id=agent.user_id,
                text="Hi! I'm here to help — can you tell me more?", created_at=now - timedelta(hours=rng.randint(0, 100)),
            ))

    await db.flush()
