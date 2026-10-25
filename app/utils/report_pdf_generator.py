import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.utils.report_document import ReportDocument

_BRAND_COLOR = colors.HexColor("#4d59d9")
_HEADER_BG = colors.HexColor("#f3f4f6")


def build_pdf_bytes(document: ReportDocument, landscape_mode: bool = False) -> bytes:
    """A functional, correctly-branded tabular report — one flowing
    document with a table per section. `landscape_mode` is used for the
    financial report, whose transactions table has too many columns
    (reference/type/source/gross/net/status/date) to read comfortably in
    portrait."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4) if landscape_mode else A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = _BRAND_COLOR
    subtitle_style = styles["Normal"]
    heading_style = styles["Heading2"]

    elements = [
        Paragraph(document.title, title_style),
        Paragraph(document.subtitle, subtitle_style),
        Paragraph(f"Generated {document.generated_at}", styles["Italic"]),
        Spacer(1, 0.5 * cm),
    ]

    for section in document.sections:
        elements.append(Paragraph(section.heading, heading_style))
        elements.append(Spacer(1, 0.2 * cm))
        table_data = [section.headers] + [[str(cell) for cell in row] for row in section.rows]
        table = Table(table_data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                ]
            )
        )
        elements.append(table)
        elements.append(Spacer(1, 0.6 * cm))

    doc.build(elements)
    return buffer.getvalue()
