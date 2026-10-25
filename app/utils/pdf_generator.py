"""Ticket PDF generator — dark cinema-ticket design matching TicketPreviewModal.

Left  42%: banner photo tint + white QR card on dark bg
Right 58%: blurred-banner backdrop + dark overlay, mirrors the JSX glass panel
           EVENT NAME → LOCATION → DATE & TIME → separator → GUEST | TYPE
"""
from datetime import date, datetime, time
from importlib import resources
from io import BytesIO
from typing import Optional

from PIL import Image, ImageFilter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

_POLLORD_LOGO_BYTES = (resources.files("app.assets") / "pollord-logo.png").read_bytes()
_POLLORD_LOGO = ImageReader(BytesIO(_POLLORD_LOGO_BYTES))

PAGE_W, PAGE_H = 595, 200

_DARK  = (0.07, 0.07, 0.09)
_WHITE = (1.00, 1.00, 1.00)
_DIM   = (0.55, 0.55, 0.60)


def _fit(w: float, h: float, max_w: float, max_h: float) -> tuple[float, float]:
    scale = min(max_w / w, max_h / h)
    return w * scale, h * scale


def _wrap(c: canvas.Canvas, text: str, font: str, size: int, max_w: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if c.stringWidth(candidate, font, size) <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _make_left_bg(banner: Optional[Image.Image], split_x: int) -> bytes:
    """Dark bg with banner bleed at low opacity for the left half."""
    dark = Image.new("RGB", (split_x, PAGE_H), (18, 18, 23))
    if banner:
        scaled = banner.resize((PAGE_W, PAGE_H), Image.LANCZOS).crop((0, 0, split_x, PAGE_H))
        scaled = scaled.convert("RGB")
        # blend at 20% opacity onto dark
        dark = Image.blend(dark, scaled, alpha=0.20)
    buf = BytesIO()
    dark.save(buf, "PNG")
    buf.seek(0)
    return buf.read()


def _make_right_bg(banner: Optional[Image.Image], split_x: int) -> bytes:
    """Blurred banner + heavy dark overlay — approximates backdrop-blur + bg-black/20."""
    rw = PAGE_W - split_x
    if banner:
        crop = banner.resize((PAGE_W, PAGE_H), Image.LANCZOS).crop((split_x, 0, PAGE_W, PAGE_H))
        blurred = crop.filter(ImageFilter.GaussianBlur(radius=10)).convert("RGBA")
    else:
        blurred = Image.new("RGBA", (rw, PAGE_H), (18, 18, 23, 255))

    # 82% dark overlay mimics the preview's gradient + bg-black/20
    overlay = Image.new("RGBA", (rw, PAGE_H), (18, 18, 23, int(0.82 * 255)))
    result = Image.alpha_composite(blurred, overlay)
    buf = BytesIO()
    result.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf.read()


def generate_ticket_pdf(
    event_title: str,
    event_date: date,
    event_time: time,
    location: str,
    ticket_type: str,
    ticket_code: str,
    attendee_name: str,
    purchase_date: datetime,
    qr_bytes: Optional[bytes] = None,
    banner_image_bytes: Optional[bytes] = None,
    org_logo_bytes: Optional[bytes] = None,
) -> bytes:
    split_x = int(PAGE_W * 0.42)

    banner_pil: Optional[Image.Image] = None
    if banner_image_bytes:
        try:
            banner_pil = Image.open(BytesIO(banner_image_bytes)).convert("RGBA")
        except Exception:
            pass

    left_bg_bytes  = _make_left_bg(banner_pil, split_x)
    right_bg_bytes = _make_right_bg(banner_pil, split_x)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))

    # ── Background panels ─────────────────────────────────────────────────
    c.drawImage(ImageReader(BytesIO(left_bg_bytes)),  0,       0, split_x,          PAGE_H)
    c.drawImage(ImageReader(BytesIO(right_bg_bytes)), split_x, 0, PAGE_W - split_x, PAGE_H)

    pad = 16

    # ── White QR card ─────────────────────────────────────────────────────
    qr_card_size = min(PAGE_H - 2 * pad - 20, split_x - 2 * pad)
    qr_card_x = (split_x - qr_card_size) / 2
    qr_card_y = PAGE_H / 2 - qr_card_size / 2
    c.setFillColorRGB(*_WHITE)
    c.roundRect(qr_card_x, qr_card_y, qr_card_size, qr_card_size, 8, stroke=0, fill=1)
    if qr_bytes:
        qr_img = ImageReader(BytesIO(qr_bytes))
        inner = qr_card_size - 12
        c.drawImage(qr_img, qr_card_x + 6, qr_card_y + 6, inner, inner)

    # ── Perforated seam ───────────────────────────────────────────────────
    notch_r = 14
    c.setFillColorRGB(*_DARK)
    c.circle(split_x, 0,       notch_r, stroke=0, fill=1)
    c.circle(split_x, PAGE_H,  notch_r, stroke=0, fill=1)
    c.setStrokeColorRGB(0.30, 0.30, 0.35)
    c.setDash(3, 5)
    c.setLineWidth(1)
    c.line(split_x, notch_r, split_x, PAGE_H - notch_r)
    c.setDash()

    # ── Right panel text ──────────────────────────────────────────────────
    rx = split_x + pad
    rw = PAGE_W - split_x - 2 * pad

    y = PAGE_H - pad  # top cursor

    # EVENT NAME
    c.setFillColorRGB(*_DIM)
    c.setFont("Helvetica", 6.5)
    c.drawString(rx, y - 8, "EVENT NAME")
    y -= 25  # extra breathing room between label and title
    title_lines = _wrap(c, (event_title or "Event").upper(), "Helvetica-Bold", 14, rw)[:2]
    c.setFillColorRGB(*_WHITE)
    c.setFont("Helvetica-Bold", 14)
    for i, line in enumerate(title_lines):
        c.drawString(rx, y - i * 16, line)
    y -= (len(title_lines) - 1) * 16 + 14

    # LOCATION
    y -= 10
    c.setFillColorRGB(*_DIM)
    c.setFont("Helvetica", 6.5)
    c.drawString(rx, y, "LOCATION")
    y -= 16  # extra breathing room between label and value
    loc_lines = _wrap(c, location or "Not set", "Helvetica-Bold", 11, rw)[:2]
    c.setFillColorRGB(*_WHITE)
    c.setFont("Helvetica-Bold", 11)
    for i, line in enumerate(loc_lines):
        c.drawString(rx, y - i * 13, line)
    y -= (len(loc_lines) - 1) * 13 + 11

    # DATE & TIME
    y -= 10
    c.setFillColorRGB(*_DIM)
    c.setFont("Helvetica", 6.5)
    c.drawString(rx, y, "DATE & TIME")
    y -= 16  # extra breathing room between label and value
    date_str = (
        f"{event_date.strftime('%a, %b %d')} · {event_time.strftime('%I:%M %p')}"
    )
    c.setFillColorRGB(*_WHITE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(rx, y, date_str)

    # ── Bottom: separator → GUEST | TYPE → Powered-by ────────────────────
    sep_y      = pad + 42   # separator
    guest_lbl_y = sep_y - 10
    guest_y    = guest_lbl_y - 13
    footer_y   = pad - 2

    c.setStrokeColorRGB(0.22, 0.22, 0.28)
    c.setLineWidth(0.5)
    c.line(rx, sep_y, rx + rw, sep_y)

    # GUEST (left)
    c.setFillColorRGB(*_DIM)
    c.setFont("Helvetica", 6.5)
    c.drawString(rx, guest_lbl_y, "GUEST")
    c.setFillColorRGB(*_WHITE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(rx, guest_y, attendee_name or "Guest")

    # TYPE (right-aligned, italic)
    type_val = ticket_type or "General"
    c.setFillColorRGB(*_DIM)
    c.setFont("Helvetica", 6.5)
    type_lbl_w = c.stringWidth("TYPE", "Helvetica", 6.5)
    c.drawString(rx + rw - type_lbl_w, guest_lbl_y, "TYPE")
    c.setFillColorRGB(*_WHITE)
    c.setFont("Helvetica-BoldOblique", 11)
    type_val_w = c.stringWidth(type_val, "Helvetica-BoldOblique", 11)
    c.drawString(rx + rw - type_val_w, guest_y, type_val)

    # Powered by footer
    fw, fh = _fit(*_POLLORD_LOGO.getSize(), 40, 12)
    c.setFillColorRGB(*_DIM)
    c.setFont("Helvetica", 6)
    powered_w = c.stringWidth("Powered by ", "Helvetica", 6)

    org_img = None
    org_w = org_h = 0.0
    if org_logo_bytes:
        try:
            org_img = ImageReader(BytesIO(org_logo_bytes))
            org_w, org_h = _fit(*org_img.getSize(), 30, 12)
        except Exception:
            org_img = None

    total_footer = (org_w + 4 if org_img else 0) + powered_w + fw
    fx = PAGE_W - pad - total_footer
    if org_img:
        c.drawImage(org_img, fx, footer_y - 2, org_w, org_h, mask="auto",
                    preserveAspectRatio=True)
        fx += org_w + 4
    c.drawString(fx, footer_y, "Powered by ")
    fx += powered_w
    c.drawImage(_POLLORD_LOGO, fx, footer_y - 2, fw, fh, mask="auto",
                preserveAspectRatio=True)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()
