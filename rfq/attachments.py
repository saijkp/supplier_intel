"""Bounded attachment extraction; unreadable documents require a human."""
import io
from pathlib import Path
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT = 100000

def extract_text(filename, payload):
    if len(payload) > MAX_FILE_BYTES:
        return ('', 'needs_manual_review')
    try:
        suffix = Path(filename).suffix.lower()
        if suffix == '.pdf':
            from pypdf import PdfReader
            text = '\n'.join((page.extract_text() or '' for page in PdfReader(io.BytesIO(payload)).pages))
        elif suffix == '.xlsx':
            from openpyxl import load_workbook
            book = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
            try:
                chunks = []
                size = 0
                for sheet in book:
                    for row in sheet.iter_rows(values_only=True):
                        chunk = '\t'.join((str(v) if v is not None else '' for v in row))
                        chunks.append(chunk)
                        size += len(chunk) + 1
                        if size >= MAX_TEXT:
                            break
                    if size >= MAX_TEXT:
                        break
                text = '\n'.join(chunks)
            finally:
                book.close()
        elif suffix in {'.csv', '.txt'}:
            text = payload.decode('utf-8-sig')
        else:
            return ('', 'needs_manual_review')
        return (text[:MAX_TEXT], 'read' if text.strip() else 'needs_manual_review')
    except Exception:
        return ('', 'needs_manual_review')
