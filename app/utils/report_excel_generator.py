import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.utils.report_document import ReportDocument, ReportSection

# Same family as report_pdf_generator.py's _BRAND_COLOR — CSV/Excel/PDF
# exports of the same report should read as siblings, not three unrelated
# tools' output.
_BRAND = "4D59D9"
_BRAND_FILL = PatternFill("solid", fgColor=_BRAND)
_STRIPE_FILL = PatternFill("solid", fgColor="F9FAFB")
_TOTAL_FILL = PatternFill("solid", fgColor="EEF0FB")
_BORDER_COLOR = "E5E7EB"
_THIN_BORDER = Border(*(Side(style="thin", color=_BORDER_COLOR) for _ in range(4)))
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_TITLE_FONT = Font(bold=True, size=15, color="111827")
_SUBTITLE_FONT = Font(size=10, color="6B7280")
_NOTE_FONT = Font(italic=True, size=9, color="6B7280")
_TOTAL_FONT = Font(bold=True, color="111827")
_MAX_COLUMN_WIDTH = 48
_MIN_COLUMN_WIDTH = 12

# Rows the table occupies below the title block.
_TITLE_ROWS = 3


def build_excel_bytes(document: ReportDocument) -> bytes:
    """One sheet per section, each with a title block then a styled table.

    Numeric columns are written as real numbers with a number format rather
    than pre-rendered strings, so the recipient can sum, sort and filter them
    — a spreadsheet of text that merely looks like money is worth very little.
    """
    workbook = Workbook()
    workbook.remove(workbook.active)

    for section in document.sections:
        sheet = workbook.create_sheet(_safe_sheet_name(section.heading))
        _write_title_block(sheet, document, section)
        _write_table(sheet, section)

    if not workbook.worksheets:  # a report with no sections still has to open
        workbook.create_sheet("Report")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _safe_sheet_name(heading: str) -> str:
    # Excel caps sheet names at 31 chars and forbids []:*?/\ — openpyxl
    # raises on both, so sanitize before use.
    return "".join(c for c in heading if c not in "[]:*?/\\")[:31] or "Sheet"


def _write_title_block(sheet, document: ReportDocument, section: ReportSection) -> None:
    sheet["A1"] = f"{document.title} — {section.heading}"
    sheet["A1"].font = _TITLE_FONT
    sheet["A2"] = f"{document.subtitle}  ·  Generated {document.generated_at}"
    sheet["A2"].font = _SUBTITLE_FONT
    if section.note:
        sheet["A3"] = section.note
        sheet["A3"].font = _NOTE_FONT
    sheet.row_dimensions[1].height = 22


def _write_table(sheet, section: ReportSection) -> None:
    # Explicit row indexes rather than append(): the title block above means
    # the table never starts at row 1, and append()'s idea of "next row"
    # drifts depending on whether the optional note line was written.
    header_row = _TITLE_ROWS + 2  # blank spacer row between title and table
    column_count = len(section.headers)

    for col_index, header in enumerate(section.headers, start=1):
        cell = sheet.cell(row=header_row, column=col_index, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _BRAND_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = _THIN_BORDER
    sheet.row_dimensions[header_row].height = 20

    for offset, row in enumerate(section.rows):
        excel_row = header_row + 1 + offset
        for col_index in range(column_count):
            value = row[col_index] if col_index < len(row) else None
            cell = sheet.cell(row=excel_row, column=col_index + 1, value=value)
            cell.border = _THIN_BORDER
            if offset % 2 == 1:
                cell.fill = _STRIPE_FILL
            fmt = section.number_formats.get(col_index)
            if fmt and isinstance(value, (int, float)):
                cell.number_format = fmt
                cell.alignment = Alignment(horizontal="right")

    last_row = header_row + len(section.rows)

    if section.total_columns and section.rows:
        _write_total_row(sheet, section, header_row, last_row, column_count)

    if section.rows:
        sheet.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(column_count)}{last_row}"
        )
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    _fit_columns(sheet, section)


def _write_total_row(sheet, section: ReportSection, header_row: int, last_row: int, column_count: int) -> None:
    total_row = last_row + 1
    sheet.cell(row=total_row, column=1, value="Total")
    for col_index in range(column_count):
        cell = sheet.cell(row=total_row, column=col_index + 1)
        cell.font = _TOTAL_FONT
        cell.fill = _TOTAL_FILL
        cell.border = _THIN_BORDER
        if col_index in section.total_columns:
            letter = get_column_letter(col_index + 1)
            # A live formula, not a baked value, so the total still holds if
            # the recipient filters or edits rows.
            cell.value = f"=SUBTOTAL(109,{letter}{header_row + 1}:{letter}{last_row})"
            fmt = section.number_formats.get(col_index)
            if fmt:
                cell.number_format = fmt
            cell.alignment = Alignment(horizontal="right")


def _fit_columns(sheet, section: ReportSection) -> None:
    for col_index, header in enumerate(section.headers):
        widest = len(str(header))
        for row in section.rows:
            if col_index < len(row) and row[col_index] is not None:
                widest = max(widest, len(str(row[col_index])))
        sheet.column_dimensions[get_column_letter(col_index + 1)].width = max(
            _MIN_COLUMN_WIDTH, min(_MAX_COLUMN_WIDTH, widest + 3)
        )
