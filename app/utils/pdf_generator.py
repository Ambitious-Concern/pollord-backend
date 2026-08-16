from datetime import date, datetime, time
from importlib import resources
from io import BytesIO

from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

_POLLORD_LOGO_BYTES = (resources.files("app.assets") / "pollord-logo.png").read_bytes()
_POLLORD_LOGO = ImageReader(BytesIO(_POLLORD_LOGO_BYTES))

PAGE_W, PAGE_H = 320, 660
MARGIN = 20
CARD_RADIUS = 22
GAP = 20  # perforated strip between the two card halves
NOTCH_R = 22  # > GAP/2 so the notch actually bites into both cards, not just the gap
TOP_CARD_H = 300
BOTTOM_CARD_H = 300

BG_COLOR = (0.30, 0.30, 0.30)
CARD_COLOR = (0.925, 0.925, 0.925)
IMAGE_PLACEHOLDER_COLOR = (0.35, 0.35, 0.35)
LABEL_COLOR = (0.45, 0.45, 0.45)
TEXT_COLOR = (0.05, 0.05, 0.05)
BRAND_COLOR = (0.3, 0.35, 0.85)


def _wrap_text(c: canvas.Canvas, text: str, font: str, size: int, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if c.stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _draw_field(c: canvas.Canvas, x: float, y: float, label: str, value: str, max_width: float) -> float:
    """Draws label+value with (x, y) as the label's baseline. Returns the y
    of the last value line's baseline, so callers can stack the next field
    below whatever this one actually rendered (including wrapped values)
    instead of guessing a fixed offset."""
    c.setFillColorRGB(*LABEL_COLOR)
    c.setFont("Helvetica", 9)
    c.drawString(x, y, label)
    c.setFillColorRGB(*TEXT_COLOR)
    c.setFont("Helvetica-Bold", 12)
    lines = _wrap_text(c, value, "Helvetica-Bold", 12, max_width)
    for i, line in enumerate(lines):
        c.drawString(x, y - 16 - (i * 14), line)
    return y - 16 - ((len(lines) - 1) * 14)


def _draw_image_box(
    c: canvas.Canvas, x: float, y: float, size: float, radius: float, image_bytes: bytes | None
) -> None:
    if not image_bytes:
        c.setFillColorRGB(*IMAGE_PLACEHOLDER_COLOR)
        c.roundRect(x, y, size, size, radius, stroke=0, fill=1)
        return

    try:
        image = ImageReader(BytesIO(image_bytes))
        img_w, img_h = image.getSize()
    except Exception:
        c.setFillColorRGB(*IMAGE_PLACEHOLDER_COLOR)
        c.roundRect(x, y, size, size, radius, stroke=0, fill=1)
        return

    c.saveState()
    clip = c.beginPath()
    clip.roundRect(x, y, size, size, radius)
    c.clipPath(clip, stroke=0, fill=0)

    # Center-crop to a square: scale so the shorter side fills the box, then
    # shift the overflowing side back by half so the crop stays centered.
    scale = max(size / img_w, size / img_h)
    draw_w, draw_h = img_w * scale, img_h * scale
    draw_x = x - (draw_w - size) / 2
    draw_y = y - (draw_h - size) / 2
    c.drawImage(image, draw_x, draw_y, width=draw_w, height=draw_h, mask="auto")
    c.restoreState()


def _fit(img_w: float, img_h: float, max_size: float) -> tuple[float, float]:
    """Scale (img_w, img_h) down to fit within a max_size square, preserving aspect."""
    scale = min(max_size / img_w, max_size / img_h)
    return img_w * scale, img_h * scale


def _draw_footer(c: canvas.Canvas, baseline_y: float, org_logo_bytes: bytes | None) -> None:
    """'[org logo] Powered by [Pollord logo]', centered as one row."""
    org_logo_image = None
    org_logo_w = org_logo_h = 0.0
    if org_logo_bytes:
        try:
            org_logo_image = ImageReader(BytesIO(org_logo_bytes))
            org_logo_w, org_logo_h = _fit(*org_logo_image.getSize(), 18)
        except Exception:
            org_logo_image = None

    pollord_w, pollord_h = _fit(*_POLLORD_LOGO.getSize(), 15)

    font, font_size = "Helvetica-Bold", 8
    text = "Powered by"
    text_w = c.stringWidth(text, font, font_size)

    gap = 6
    org_block_w = (org_logo_w + gap) if org_logo_image else 0
    total_w = org_block_w + text_w + gap + pollord_w
    x = PAGE_W / 2 - total_w / 2

    if org_logo_image:
        c.drawImage(
            org_logo_image, x, baseline_y - 2, width=org_logo_w, height=org_logo_h,
            preserveAspectRatio=True, mask="auto",
        )
        x += org_block_w

    c.setFillColorRGB(*BRAND_COLOR)
    c.setFont(font, font_size)
    c.drawString(x, baseline_y, text)
    x += text_w + gap

    c.drawImage(
        _POLLORD_LOGO, x, baseline_y - 2, width=pollord_w, height=pollord_h,
        preserveAspectRatio=True, mask="auto",
    )


def generate_ticket_pdf(
    event_title: str,
    event_date: date,
    event_time: time,
    location: str,
    ticket_type: str,
    ticket_code: str,
    attendee_name: str,
    purchase_date: datetime,
    qr_bytes: bytes | None = None,
    banner_image_bytes: bytes | None = None,
    org_logo_bytes: bytes | None = None,
) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))

    # Page background
    c.setFillColorRGB(*BG_COLOR)
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)

    card_x = MARGIN
    card_w = PAGE_W - 2 * MARGIN
    top_card_h = TOP_CARD_H
    top_card_y = PAGE_H - MARGIN - top_card_h
    bottom_card_h = BOTTOM_CARD_H
    bottom_card_y = top_card_y - GAP - bottom_card_h

    # --- Top card ---
    c.setFillColorRGB(*CARD_COLOR)
    c.roundRect(card_x, top_card_y, card_w, top_card_h, CARD_RADIUS, stroke=0, fill=1)

    pad = 18
    image_size = 110
    image_x = card_x + pad
    image_y = top_card_y + top_card_h - pad - image_size
    _draw_image_box(c, image_x, image_y, image_size, 12, banner_image_bytes)

    title_x = image_x + image_size + 16
    title_max_w = card_x + card_w - pad - title_x
    c.setFillColorRGB(*TEXT_COLOR)
    title_lines = _wrap_text(c, event_title or "Event", "Helvetica-Bold", 20, title_max_w)[:2]
    title_top_y = top_card_y + top_card_h - pad - 20
    for i, line in enumerate(title_lines):
        c.setFont("Helvetica-Bold", 20)
        c.drawString(title_x, title_top_y - (i * 24), line)

    event_dt_label = f"{event_date.strftime('%a')}, {event_time.strftime('%I:%M%p').lower()}"
    text_y = title_top_y - ((len(title_lines) - 1) * 24) - 24
    text_y = _draw_field(c, title_x, text_y, "Attendee:", attendee_name or "Guest", title_max_w) - 28
    text_y = _draw_field(c, title_x, text_y, "Event Time:", event_dt_label, title_max_w)

    col_w = (card_w - 2 * pad) / 2
    col1_x = card_x + pad
    col2_x = card_x + pad + col_w
    # Whichever is taller — the image or the (variable-height, depending on
    # how much the title/attendee/event-time text wrapped) text stack next
    # to it — the info grid below must clear both, or long titles overlap
    # "Event Time" with "Date of Booking"/"Event Venue".
    header_bottom_y = min(image_y, text_y)
    row1_y = header_bottom_y - 30
    row2_y = row1_y - 60

    booking_date = purchase_date.strftime("%b %d, %Y")
    booking_time = purchase_date.strftime("%I:%M%p").lower() + " GMT"

    _draw_field(c, col1_x, row1_y, "Date of Booking", booking_date, col_w - 8)
    _draw_field(c, col2_x, row1_y, "Event Venue:", location or "TBA", col_w - 8)
    _draw_field(c, col1_x, row2_y, "Time of Booking:", booking_time, col_w - 8)
    _draw_field(c, col2_x, row2_y, "Ticket Type:", ticket_type or "General", col_w - 8)

    # --- Bottom card (drawn before the seam so the notches punch through both cards) ---
    c.setFillColorRGB(*CARD_COLOR)
    c.roundRect(card_x, bottom_card_y, card_w, bottom_card_h, CARD_RADIUS, stroke=0, fill=1)

    # --- Perforated seam ---
    # Notches sit right on the card's own side edges (not the page edges), sized
    # bigger than the gap itself so they visibly bite into both cards.
    seam_y = top_card_y - GAP / 2
    c.setFillColorRGB(*BG_COLOR)
    c.circle(card_x, seam_y, NOTCH_R, stroke=0, fill=1)
    c.circle(card_x + card_w, seam_y, NOTCH_R, stroke=0, fill=1)
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setDash(3, 4)
    c.setLineWidth(1)
    c.line(card_x + NOTCH_R, seam_y, card_x + card_w - NOTCH_R, seam_y)
    c.setDash()

    qr_size = 180
    qr_x = card_x + (card_w - qr_size) / 2
    qr_y = bottom_card_y + bottom_card_h - pad - qr_size
    if qr_bytes:
        qr_image = ImageReader(BytesIO(qr_bytes))
        c.drawImage(qr_image, qr_x, qr_y, width=qr_size, height=qr_size)

    c.setFillColorRGB(*LABEL_COLOR)
    c.setFont("Helvetica", 8)
    c.drawCentredString(PAGE_W / 2, qr_y - 24, "TICKET CODE")
    c.setFillColorRGB(*TEXT_COLOR)
    c.setFont("Courier-Bold", 14)
    c.drawCentredString(PAGE_W / 2, qr_y - 42, ticket_code)

    _draw_footer(c, bottom_card_y + 24, org_logo_bytes)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()
