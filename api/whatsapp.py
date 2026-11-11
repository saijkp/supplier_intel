"""Authenticated manual WhatsApp screenshots; tokens never appear in image URLs."""
from fastapi import APIRouter, Depends, File, Form, UploadFile, Response
from fastapi.responses import FileResponse
from api.auth import require_api_token
from customers.whatsapp_inbox import WhatsAppInbox
from pydantic import BaseModel, Field
from fastapi import HTTPException
from api.customers import get_customer_service, _call
from storage.database import connection_scope

router = APIRouter(prefix='/customers/whatsapp', tags=['Customers'], dependencies=[Depends(require_api_token)])

def get_whatsapp_inbox():
    return WhatsAppInbox()

@router.get('')
def cards(response: Response, inbox=Depends(get_whatsapp_inbox)):
    response.headers['Cache-Control'] = 'no-store'
    return inbox.cards()

@router.post('')
async def upload(files: list[UploadFile] = File(...), text: str = Form(''), enquiry_id: int | None = Form(None), inbox=Depends(get_whatsapp_inbox)):
    return await inbox.create(files, text, enquiry_id)

@router.get('/images/{image_id}')
def image(image_id: int, inbox=Depends(get_whatsapp_inbox)):
    path, media_type = inbox.image(image_id)
    return FileResponse(path, media_type=media_type, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

@router.get('/email-cards')
def email_cards(response: Response, inbox=Depends(get_whatsapp_inbox)):
    response.headers['Cache-Control'] = 'no-store'
    return inbox.email_cards()


class ScreenshotEnquiry(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    phone: str = Field(default='', max_length=60)
    title: str = Field(default='WhatsApp enquiry', min_length=1, max_length=300)


@router.post('/{card_id}/convert')
def convert_screenshot(card_id: int, body: ScreenshotEnquiry, inbox=Depends(get_whatsapp_inbox),
                       service=Depends(get_customer_service)):
    from customers.customer_service import _FORM_INTAKE_LOCK
    # Manual transcription only. Screenshots never trigger OCR or a paid model.
    with _FORM_INTAKE_LOCK:
        card = next((c for c in inbox.cards() if c['id'] == card_id), None)
        if card is None:
            raise HTTPException(404, 'Screenshot not found')
        if card['enquiry_id']:
            return service.get_enquiry(card['enquiry_id'])
        matches = service.repo.rows('customers', 'phone=? OR whatsapp=?', (body.phone, body.phone)) if body.phone else []
        customer = matches[0] if matches else _call(service.create_customer, name=body.name, phone=body.phone, source='WhatsApp')
        enquiries = service.get_customer(customer['id'])['enquiries']
        enquiry = next((e for e in enquiries if e['status'] not in {'won','lost','closed'}), None)
        enquiry = enquiry or _call(service.create_enquiry, customer['id'], title=body.title, next_action='Reply to WhatsApp enquiry')
        _call(service.log, enquiry['id'], 'whatsapp_in', card['text'] or 'WhatsApp screenshot received; see linked attachment')
        with connection_scope(inbox.db_path) as conn:
            conn.execute('UPDATE whatsapp_cards SET enquiry_id=? WHERE id=? AND enquiry_id IS NULL', (enquiry['id'], card_id))
        return service.get_enquiry(enquiry['id'])
