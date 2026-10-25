"""Shared intermediate shape every report-export generator (CSV/Excel/PDF)
consumes — report-type-specific logic (voting/financial/attendance) lives
entirely in report_export_runner, which builds one of these; the generators
themselves know nothing about what a "category" or "ticket" is."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

Cell = Union[str, int, float, None]

# openpyxl number formats, named so assemblers don't repeat format strings.
MONEY = '#,##0.00'
INTEGER = '#,##0'
PERCENT = '0.0"%"'


@dataclass
class ReportSection:
    heading: str
    headers: List[str]
    rows: List[List[Cell]]
    # 0-based column index -> openpyxl number format. Money and counts are
    # written as real numbers rather than pre-formatted strings, so the sheet
    # can actually sum, sort and filter them.
    number_formats: Dict[int, str] = field(default_factory=dict)
    # 0-based column indexes to total in a footer row.
    total_columns: List[int] = field(default_factory=list)
    # Shown under the heading — e.g. "580 transactions".
    note: Optional[str] = None


@dataclass
class ReportDocument:
    title: str
    subtitle: str
    generated_at: str
    sections: List[ReportSection] = field(default_factory=list)
