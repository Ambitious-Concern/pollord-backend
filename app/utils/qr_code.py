import json
from io import BytesIO
from uuid import UUID

import qrcode


def generate_qr_code(data: str) -> bytes:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def generate_ticket_qr_data(
    ticket_code: str, event_id: UUID, ticket_type: str
) -> str:
    return json.dumps(
        {
            "ticket_code": ticket_code,
            "event_id": str(event_id),
            "ticket_type": ticket_type,
        }
    )
