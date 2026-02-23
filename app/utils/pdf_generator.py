from io import BytesIO

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


def generate_ticket_pdf(
    event_title: str,
    event_date: str,
    event_time: str,
    location: str,
    ticket_type: str,
    ticket_code: str,
    attendee_name: str,
    qr_bytes: bytes = None,
) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Header
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2, height - 1 * inch, "EVENT TICKET")

    # Event title
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width / 2, height - 1.6 * inch, event_title)

    # Divider
    c.setStrokeColorRGB(0.2, 0.2, 0.2)
    c.line(1 * inch, height - 1.8 * inch, width - 1 * inch, height - 1.8 * inch)

    # Details
    c.setFont("Helvetica", 12)
    y = height - 2.3 * inch

    details = [
        ("Date:", event_date),
        ("Time:", event_time),
        ("Location:", location),
        ("Ticket Type:", ticket_type),
        ("Attendee:", attendee_name),
        ("Ticket Code:", ticket_code),
    ]

    for label, value in details:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(1.5 * inch, y, label)
        c.setFont("Helvetica", 12)
        c.drawString(3.5 * inch, y, value)
        y -= 0.35 * inch

    # QR Code
    if qr_bytes:
        from reportlab.lib.utils import ImageReader

        qr_image = ImageReader(BytesIO(qr_bytes))
        c.drawImage(
            qr_image,
            width / 2 - 1 * inch,
            y - 2.5 * inch,
            width=2 * inch,
            height=2 * inch,
        )

    # Footer
    c.setFont("Helvetica", 8)
    c.drawCentredString(
        width / 2, 0.5 * inch, "Powered by Pollard Platform"
    )

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()
