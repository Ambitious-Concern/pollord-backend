"""The generate side of a report export job — runs inside the Celery task
(see app/tasks/report_export_tasks.py), never called directly from a
request. Assembles the report-type-specific data using the same services
the live dashboard endpoints use, then renders + uploads the file."""
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import async_session_maker
from app.models.audit_log import AuditLog
from app.models.election import Candidate, Category, Election
from app.models.event import Event, TicketType
from app.models.organization import Organization
from app.models.report_export_job import ReportExportJob
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.report_export_job_repository import ReportExportJobRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.user_repository import UserRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.services.cryptography_service import CryptographyService
from app.services.file_storage_service import file_storage_service
from app.services.financial_stats_service import FinancialStatsService
from app.services.ticketing_service import TicketingService
from app.services.voting_service import VotingService
from app.utils.report_csv_generator import build_csv_bytes
from app.utils.report_document import INTEGER, MONEY, PERCENT, ReportDocument, ReportSection
from app.utils.report_excel_generator import build_excel_bytes
from app.utils.report_pdf_generator import build_pdf_bytes

_CONTENT_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


async def run_export_job(job_id: UUID, db: Optional[AsyncSession] = None) -> None:
    """db is injectable purely for tests — the real Celery task always
    calls this with no session, getting its own short-lived one, since a
    background job must never borrow a request-scoped session."""
    if db is not None:
        await _run(job_id, db)
        return
    async with async_session_maker() as fresh_db:
        await _run(job_id, fresh_db)


async def _run(job_id: UUID, db: AsyncSession) -> None:
    jobs = ReportExportJobRepository(ReportExportJob, db)
    job = await jobs.get_by_id(job_id, id_field="job_id")
    if not job:
        return

    await jobs.mark_processing(job_id)
    await db.commit()

    try:
        document = await _assemble(db, job)
        content = _render(job.format, job.report_type, document)
        filename = f"{job.report_type}-report-{job.job_id}.{job.format}"
        download_url = await file_storage_service.upload(content, filename, _CONTENT_TYPES[job.format])
        await jobs.mark_completed(job_id, download_url)
    except Exception as exc:  # noqa: BLE001 — any failure just marks the job failed
        await jobs.mark_failed(job_id, str(exc))
    await db.commit()


def _render(fmt: str, report_type: str, document: ReportDocument) -> bytes:
    if fmt == "csv":
        return build_csv_bytes(document)
    if fmt == "xlsx":
        return build_excel_bytes(document)
    # The financial transactions table (reference/type/source/gross/net/
    # status/date) has too many columns to read comfortably in portrait.
    return build_pdf_bytes(document, landscape_mode=report_type == "financial")


async def _assemble(db, job: ReportExportJob) -> ReportDocument:
    if job.report_type == "voting":
        return await _assemble_voting(db, job.params)
    if job.report_type == "financial":
        return await _assemble_financial(db, job.requested_by, job.params)
    return await _assemble_attendance(db, job.requested_by, job.params)


_PAGE = 500


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _voting_service(db) -> VotingService:
    return VotingService(
        election_repo=ElectionRepository(Election, db),
        event_repo=EventRepository(Event, db),
        category_repo=CategoryRepository(Category, db),
        candidate_repo=CandidateRepository(Candidate, db),
        vote_repo=VoteRepository(Vote, db),
        receipt_repo=VoteReceiptRepository(VoteReceipt, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        crypto_service=CryptographyService(),
    )


async def _assemble_voting(db, params: dict) -> ReportDocument:
    source_kind = params["source_kind"]
    source_id = UUID(str(params["source_id"]))
    service = _voting_service(db)
    if source_kind == "election":
        results = await service.get_live_results(source_id)
        subject = "Election"
    else:
        results = await service.get_event_live_results(source_id)
        subject = "Event"

    # Overview first: who won each category and by how much, which is the
    # question the results page is actually answering on screen.
    overview_rows = []
    for category in results.categories:
        ranked = list(category.results)
        winner = ranked[0] if ranked else None
        runner_up = ranked[1] if len(ranked) > 1 else None
        margin = (winner.vote_count - runner_up.vote_count) if winner and runner_up else None
        overview_rows.append([
            category.name,
            len(ranked),
            sum(r.vote_count for r in ranked),
            winner.name if winner else "—",
            winner.vote_count if winner else 0,
            round(winner.percentage, 1) if winner else 0,
            margin if margin is not None else "—",
        ])

    sections = [
        ReportSection(
            heading="Overview",
            headers=["Category", "Nominees", "Votes cast", "Leader", "Leader votes", "Leader share", "Margin"],
            rows=overview_rows,
            number_formats={1: INTEGER, 2: INTEGER, 4: INTEGER, 5: PERCENT, 6: INTEGER},
            total_columns=[2],
            note=f"{len(results.categories)} categories · {results.total_votes} total votes",
        )
    ]

    # Then the full per-category breakdown, one sheet each.
    all_rows = []
    for category in results.categories:
        rows = [
            [i + 1, r.name, r.vote_count, round(r.percentage, 1)]
            for i, r in enumerate(category.results)
        ]
        sections.append(
            ReportSection(
                heading=category.name,
                headers=["Rank", "Nominee", "Votes", "Share"],
                rows=rows,
                number_formats={2: INTEGER, 3: PERCENT},
                total_columns=[2],
                note=f"{len(rows)} nominees",
            )
        )
        all_rows.extend([category.name, *row] for row in rows)

    # A single flat table of every nominee across every category, so the data
    # is usable as one range without stitching the per-category sheets back
    # together.
    sections.append(
        ReportSection(
            heading="All nominees",
            headers=["Category", "Rank", "Nominee", "Votes", "Share"],
            rows=all_rows,
            number_formats={3: INTEGER, 4: PERCENT},
            total_columns=[3],
            note=f"{len(all_rows)} rows across {len(results.categories)} categories",
        )
    )

    return ReportDocument(
        title=f"Voting Results — {results.title}",
        subtitle=f"{subject} · {results.total_votes} total votes",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        sections=sections,
    )


async def _assemble_financial(db, user_id: UUID, params: dict) -> ReportDocument:
    range_id = params.get("range", "all-time")
    source_id = UUID(str(params["source_id"])) if params.get("source_id") else None
    stats = await FinancialStatsService(db).get_stats(user_id, range_id=range_id, source_id=source_id)

    summary_rows = [
        ["Gross revenue", float(stats.summary.gross_ghs)],
        ["Platform commission & fees", float(stats.summary.platform_fee_ghs)],
        ["Payment processing", float(stats.summary.processing_fee_ghs)],
        ["Net take-home", float(stats.summary.net_ghs)],
        ["Transactions", stats.summary.count],
    ]

    # Votes vs tickets, matching the "Revenue by type" card on the page.
    votes_total = float(sum(b.votes_ghs for b in stats.time_series))
    tickets_total = float(sum(b.tickets_ghs for b in stats.time_series))
    by_type_rows = [["Votes", votes_total], ["Tickets", tickets_total]]

    channel_rows = [[c.channel, float(c.amount_ghs)] for c in stats.by_channel]

    # Transactions below include every status, so surface the split: without
    # it the sheet's own total (all rows) silently disagrees with Summary,
    # which counts successful charges only.
    status_totals: dict = {}
    for t in stats.transactions:
        entry = status_totals.setdefault(t.status, {"count": 0, "gross": 0.0})
        entry["count"] += 1
        entry["gross"] += float(t.amount_ghs)
    status_rows = [
        [status, v["count"], round(v["gross"], 2)] for status, v in sorted(status_totals.items())
    ]
    source_rows = [
        [t.source_name, float(t.gross_ghs), float(t.net_ghs)] for t in stats.top_sources
    ]

    # Every column the row actually carries — the fee split and channel were
    # being dropped, which is exactly the detail a finance export is for.
    txn_headers = [
        "Date", "Reference", "Type", "Source", "Source kind", "Detail",
        "Channel", "Status", "Gross (GH₵)", "Platform fee (GH₵)",
        "Processing fee (GH₵)", "Net (GH₵)",
    ]
    txn_rows = [
        [
            t.occurred_at.strftime("%Y-%m-%d %H:%M"),
            t.reference, t.type, t.source_name, t.source_kind, t.detail,
            t.channel, t.status,
            float(t.amount_ghs), float(t.platform_fee_ghs),
            float(t.processing_fee_ghs), float(t.net_ghs),
        ]
        for t in stats.transactions
    ]
    txn_money = {8: MONEY, 9: MONEY, 10: MONEY, 11: MONEY}

    sections = [
        ReportSection(
            heading="Summary",
            headers=["Metric", "Value"],
            rows=summary_rows,
            number_formats={1: MONEY},
        ),
        ReportSection(
            heading="Revenue by type",
            headers=["Type", "Gross (GH₵)"],
            rows=by_type_rows,
            number_formats={1: MONEY},
            total_columns=[1],
        ),
        ReportSection(
            heading="By channel",
            headers=["Channel", "Gross (GH₵)"],
            rows=channel_rows,
            number_formats={1: MONEY},
            total_columns=[1],
        ),
        ReportSection(
            heading="By status",
            headers=["Status", "Transactions", "Gross (GH₵)"],
            rows=status_rows,
            number_formats={1: INTEGER, 2: MONEY},
            total_columns=[1, 2],
            note="Summary counts successful charges only; this covers every status.",
        ),
        ReportSection(
            heading="Top sources",
            headers=["Source", "Gross (GH₵)", "Net (GH₵)"],
            rows=source_rows,
            number_formats={1: MONEY, 2: MONEY},
            total_columns=[1, 2],
        ),
        ReportSection(
            heading="Transactions",
            headers=txn_headers,
            rows=txn_rows,
            number_formats=txn_money,
            total_columns=[8, 9, 10, 11],
            note=(
                f"All {len(txn_rows)} transactions, every status. The total row sums "
                "whatever is currently filtered — filter Status to 'success' to "
                "reconcile with the Summary sheet."
            ),
        ),
    ]

    return ReportDocument(
        title="Financial / Revenue Report",
        subtitle=f"Range: {range_id}",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        sections=sections,
    )


async def _assemble_attendance(db, user_id: UUID, params: dict) -> ReportDocument:
    event_id = UUID(str(params["event_id"]))
    event = await EventRepository(Event, db).get_by_id(event_id, id_field="event_id")
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(user_id)

    service = TicketingService(
        event_repo=EventRepository(Event, db),
        ticket_type_repo=TicketTypeRepository(TicketType, db),
        ticket_repo=TicketRepository(Ticket, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        user_repo=UserRepository(User, db),
    )
    # Paged through rather than capped: an export that silently stops at the
    # first N attendees is worse than a slow one, because nothing on the file
    # says it is incomplete.
    sales = []
    page = 0
    while True:
        batch = await service.get_organizer_ticket_sales(
            teammate_ids, event_id=event_id, skip=page * _PAGE, limit=_PAGE
        )
        sales.extend(batch)
        if len(batch) < _PAGE:
            break
        page += 1

    checked_in = sum(1 for s in sales if s.ticket_status == "used")
    revenue = sum(float(s.amount or 0) for s in sales)

    by_channel: dict = {}
    by_type: dict = {}
    by_status: dict = {}
    for s in sales:
        key = s.check_in_channel or "not checked in"
        by_channel[key] = by_channel.get(key, 0) + 1
        entry = by_type.setdefault(s.ticket_type_name, {"count": 0, "revenue": 0.0, "used": 0})
        entry["count"] += 1
        entry["revenue"] += float(s.amount or 0)
        if s.ticket_status == "used":
            entry["used"] += 1
        by_status[s.ticket_status] = by_status.get(s.ticket_status, 0) + 1

    rate = (checked_in / len(sales) * 100) if sales else 0.0
    summary_rows = [
        ["Total attendees", len(sales)],
        ["Checked in", checked_in],
        ["Not checked in", len(sales) - checked_in],
        ["Check-in rate", round(rate, 1)],
        ["Ticket revenue", round(revenue, 2)],
    ]

    attendee_headers = [
        "Name", "Email", "Phone", "Ticket type", "Ticket code", "Status",
        "Check-in channel", "Checked in at", "Coming from", "Payment method",
        "Purchased", "Amount (GH₵)",
    ]
    attendee_rows = [
        [
            s.attendee_name, s.attendee_email, s.attendee_phone or "",
            s.ticket_type_name, s.ticket_code, s.ticket_status,
            s.check_in_channel or "", _fmt_dt(s.used_at), s.origin or "",
            s.payment_method or "", _fmt_dt(s.purchase_date),
            float(s.amount or 0),
        ]
        for s in sales
    ]

    sections = [
        ReportSection(
            heading="Summary",
            headers=["Metric", "Value"],
            rows=summary_rows,
        ),
        ReportSection(
            heading="By ticket type",
            headers=["Ticket type", "Sold", "Checked in", "Revenue (GH₵)"],
            rows=[[k, v["count"], v["used"], round(v["revenue"], 2)] for k, v in by_type.items()],
            number_formats={1: INTEGER, 2: INTEGER, 3: MONEY},
            total_columns=[1, 2, 3],
        ),
        ReportSection(
            heading="By check-in channel",
            headers=["Channel", "Tickets"],
            rows=[[k, v] for k, v in by_channel.items()],
            number_formats={1: INTEGER},
            total_columns=[1],
        ),
        ReportSection(
            heading="By status",
            headers=["Status", "Tickets"],
            rows=[[k, v] for k, v in by_status.items()],
            number_formats={1: INTEGER},
            total_columns=[1],
        ),
        ReportSection(
            heading="Attendees",
            headers=attendee_headers,
            rows=attendee_rows,
            number_formats={11: MONEY},
            total_columns=[11],
            note=f"{len(attendee_rows)} attendees",
        ),
    ]

    return ReportDocument(
        title=f"Attendance / Check-in Report — {event.title if event else ''}",
        subtitle=f"{len(sales)} tickets · {checked_in} checked in ({rate:.1f}%)",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        sections=sections,
    )
