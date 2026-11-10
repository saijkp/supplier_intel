"""Manual intake only. Future OCR hook: explicitly opt in here; never run AI on upload."""
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import warnings

from fastapi import HTTPException
from PIL import Image, ImageOps
from config import settings as cfg
from storage.database import connection_scope

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 10
TYPES = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/heic': '.heic', 'image/heif': '.heic', 'image/webp': '.webp'}


class WhatsAppInbox:
    def __init__(self, db_path=None):
        self.db_path = db_path or cfg.DB_PATH
        # Railway DB lives on its persistent volume; keep attachments on the same volume.
        self.directory = Path(self.db_path).resolve().parent / 'whatsapp-images'

    def cards(self):
        with connection_scope(self.db_path) as conn:
            rows = conn.execute('SELECT w.*, c.name AS customer_name, e.title FROM whatsapp_cards w LEFT JOIN customer_enquiries e ON e.id=w.enquiry_id LEFT JOIN customers c ON c.id=e.customer_id ORDER BY w.created_at DESC, w.id DESC').fetchall()
            return [dict(row, images=[dict(i) for i in conn.execute('SELECT id,media_type FROM whatsapp_images WHERE card_id=?', (row['id'],))]) for row in rows]

    async def create(self, files, text='', enquiry_id=None):
        if not 1 <= len(files) <= MAX_IMAGES:
            raise HTTPException(400, 'Choose 1–10 screenshots')
        if len(text) > 20000:
            raise HTTPException(400, 'Pasted text is too long')
        prepared = []
        for upload in files:
            media = upload.content_type
            if media not in TYPES:
                raise HTTPException(415, 'JPEG, PNG, HEIC and WebP only')
            data = await upload.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(413, 'Each screenshot must be 10 MB or smaller')
            # Validate actual bytes, not just the untrusted MIME header/filename.
            heic = len(data) >= 16 and data[4:8] == b'ftyp' and any(b in data[8:32] for b in (b'heic', b'heix', b'hevc', b'hevx'))
            if media in ('image/heic', 'image/heif'):
                if not heic:
                    raise HTTPException(415, 'Invalid HEIC image')
                # Pillow without a HEIF plugin cannot decode HEIC. Preserve it for manual review.
            else:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', Image.DecompressionBombWarning)
                        with Image.open(BytesIO(data)) as image:
                            expected = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP'}[media]
                            if image.format != expected:
                                raise ValueError('Image type mismatch')
                            image.load()
                            image = ImageOps.exif_transpose(image)
                            image.thumbnail((2048, 2048))
                            output = BytesIO()
                            image.convert('RGB').save(output, 'JPEG', quality=88)
                            data, media = output.getvalue(), 'image/jpeg'
                except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                    raise HTTPException(415, 'Invalid or unsafe image') from None
            prepared.append((uuid4().hex + TYPES[media], media, data))
        written = []
        try:
            with connection_scope(self.db_path) as conn:
                if enquiry_id is not None and not conn.execute('SELECT id FROM customer_enquiries WHERE id=?', (enquiry_id,)).fetchone():
                    raise HTTPException(404, 'Enquiry not found')
                self.directory.mkdir(parents=True, exist_ok=True)
                card_id = conn.execute('INSERT INTO whatsapp_cards(text,enquiry_id) VALUES (?,?)', (text, enquiry_id)).lastrowid
                for filename, media, data in prepared:
                    path = self.directory / filename
                    path.write_bytes(data)
                    written.append(path)
                    conn.execute('INSERT INTO whatsapp_images(card_id,filename,media_type) VALUES (?,?,?)', (card_id, filename, media))
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return next(c for c in self.cards() if c['id'] == card_id)

    def image(self, image_id):
        with connection_scope(self.db_path) as conn:
            row = conn.execute('SELECT filename,media_type FROM whatsapp_images WHERE id=?', (image_id,)).fetchone()
        if row is None or not (self.directory / row['filename']).is_file():
            raise HTTPException(404, 'Image not found')
        return self.directory / row['filename'], row['media_type']

    def email_cards(self):
        with connection_scope(self.db_path) as conn:
            leads = [dict(r) for r in conn.execute("SELECT id, mailbox, coalesce(nullif(from_name,''),from_address) AS customer_name, subject, coalesce(full_text,excerpt) AS text, coalesce(received_at,created_at) AS created_at, already_replied FROM customer_inbox_leads WHERE status='new'")]
            activities = [dict(r) for r in conn.execute("SELECT a.id,CASE WHEN a.kind='whatsapp_in' THEN 'whatsapp' ELSE a.mailbox END AS mailbox,c.name AS customer_name,e.title AS subject,a.summary AS text,a.occurred_at AS created_at,a.enquiry_id, EXISTS(SELECT 1 FROM customer_activities later WHERE later.enquiry_id=a.enquiry_id AND later.kind IN ('email_out','whatsapp_out') AND later.occurred_at>a.occurred_at) AS already_replied FROM customer_activities a JOIN customer_enquiries e ON e.id=a.enquiry_id JOIN customers c ON c.id=e.customer_id WHERE a.kind IN ('email_in','whatsapp_in') ORDER BY a.occurred_at DESC LIMIT 200")]
        return leads + activities
