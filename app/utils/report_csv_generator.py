import csv
import io

from app.utils.report_document import ReportDocument


def build_csv_bytes(document: ReportDocument) -> bytes:
    """Every section of the report, one block after another.

    CSV has no concept of sheets, so sections are written sequentially with
    their heading above each block and a blank line between. Previously only
    the single most-detailed table was emitted, which silently dropped the
    summary and breakdown tables that the Excel and PDF versions of the same
    report both include.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow([document.title])
    if document.subtitle:
        writer.writerow([document.subtitle])
    writer.writerow([f"Generated {document.generated_at}"])

    for section in document.sections:
        writer.writerow([])
        writer.writerow([section.heading])
        if section.note:
            writer.writerow([section.note])
        writer.writerow(section.headers)
        for row in section.rows:
            writer.writerow(["" if cell is None else cell for cell in row])

    # UTF-8 BOM so Excel opens GH₵/accented names without mangling them —
    # same reasoning as the client-side downloadCsv utility.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
