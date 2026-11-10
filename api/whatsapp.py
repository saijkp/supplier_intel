"""Authenticated manual WhatsApp screenshots; tokens never appear in image URLs."""
from fastapi import APIRouter, Depends, File, Form, UploadFile, Response
from fastapi.responses import FileResponse
from api.auth import require_api_token
from customers.whatsapp_inbox import WhatsAppInbox

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
