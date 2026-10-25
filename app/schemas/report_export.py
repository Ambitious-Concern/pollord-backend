from datetime import datetime
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel

ReportType = Literal["voting", "financial", "attendance"]
ReportFormat = Literal["csv", "xlsx", "pdf"]
ReportJobStatus = Literal["pending", "processing", "completed", "failed"]


class ReportExportRequest(BaseModel):
    report_type: ReportType
    format: ReportFormat
    # Shape depends on report_type — see ReportExportService:
    #   voting:     {source_kind: "election"|"event", source_id: UUID}
    #   financial:  {range: "this-month"|"last-30"|"last-90"|"all-time", source_id?: UUID}
    #   attendance: {event_id: UUID}
    params: Dict[str, Any] = {}
    # Optional client-supplied key for explicit control; omitted, a
    # deterministic hash of (report_type, format, params) is used instead
    # so an accidental duplicate submit is still caught.
    idempotency_key: Optional[str] = None


class ReportExportJobResponse(BaseModel):
    job_id: UUID
    report_type: ReportType
    format: ReportFormat
    status: ReportJobStatus
    download_url: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
