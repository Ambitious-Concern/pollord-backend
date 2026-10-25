from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel

TransactionType = Literal["vote", "ticket"]
TransactionStatus = Literal["success", "pending", "failed"]
DateRange = Literal["this-month", "last-30", "last-90", "all-time"]


class FinancialTransactionResponse(BaseModel):
    id: str
    type: TransactionType
    source_id: UUID
    source_name: str
    source_kind: Literal["election", "event"]
    detail: str
    amount_ghs: Decimal
    platform_fee_ghs: Decimal
    processing_fee_ghs: Decimal
    net_ghs: Decimal
    channel: str
    status: TransactionStatus
    reference: str
    occurred_at: datetime


class FinancialSummary(BaseModel):
    gross_ghs: Decimal = Decimal("0")
    platform_fee_ghs: Decimal = Decimal("0")
    processing_fee_ghs: Decimal = Decimal("0")
    net_ghs: Decimal = Decimal("0")
    count: int = 0


class FinancialChannelBreakdown(BaseModel):
    channel: str
    amount_ghs: Decimal


class FinancialSourceBreakdown(BaseModel):
    source_id: UUID
    source_name: str
    gross_ghs: Decimal
    net_ghs: Decimal


class FinancialTimeBucket(BaseModel):
    label: str
    votes_ghs: Decimal
    tickets_ghs: Decimal


class FinancialBreakdownItem(BaseModel):
    key: str
    label: str
    value: Decimal


class FinancialStatsResponse(BaseModel):
    range: DateRange
    summary: FinancialSummary
    previous_summary: FinancialSummary
    by_channel: List[FinancialChannelBreakdown]
    top_sources: List[FinancialSourceBreakdown]
    time_series: List[FinancialTimeBucket]
    transactions: List[FinancialTransactionResponse]
    # Only populated when source_id scopes results to one election or event —
    # top_sources trivially collapses to a single row there, so this carries a
    # more useful breakdown instead: categories by revenue for an election,
    # ticket types by quantity sold for an event.
    scoped_breakdown: List[FinancialBreakdownItem] = []
    scoped_breakdown_kind: Optional[Literal["categories", "ticket_types"]] = None
