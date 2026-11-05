"""Public HMAC-authenticated webhook; private token-authenticated review endpoints."""
import hashlib
import hmac
import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Request, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from api.auth import require_api_token
from rfq.whatsapp_config import configuration
from rfq.whatsapp_webhook import parse_payload
from storage.whatsapp_repository import WhatsAppRepository

router = APIRouter()
logger = logging.getLogger(__name__)
MAX_BODY = 256 * 1024


def enabled(config=Depends(configuration)):
    if not config['enabled']:
        raise HTTPException(404, 'Not found')
    if not config['secret'] or not config['verify']:
        raise HTTPException(503, 'Webhook not configured')
    return config


def get_repo():
    return WhatsAppRepository()


@router.get('/webhooks/whatsapp', response_class=PlainTextResponse)
def verify(request: Request, config=Depends(enabled)):
    params = request.query_params
    if any(len(params.getlist(k)) != 1 for k in ('hub.mode','hub.verify_token','hub.challenge')):
        raise HTTPException(400, 'Invalid verification')
    challenge = params['hub.challenge']
    if params['hub.mode'] != 'subscribe' or not hmac.compare_digest(params['hub.verify_token'].encode(), config['verify'].encode()):
        raise HTTPException(403, 'Verification failed')
    if not re.fullmatch(r'[0-9]{1,128}', challenge):
        raise HTTPException(400, 'Invalid challenge')
    return challenge


@router.post('/webhooks/whatsapp')
async def receive(request: Request, config=Depends(enabled), repo=Depends(get_repo)):
    if request.headers.get('content-type','').split(';')[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'JSON required')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>MAX_BODY:
            raise HTTPException(413, 'Payload too large')
    signatures = request.headers.getlist('x-hub-signature-256')
    expected = 'sha256=' + hmac.new(config['secret'].encode(), raw, hashlib.sha256).hexdigest()
    if len(signatures)!=1 or not hmac.compare_digest(signatures[0].encode(),expected.encode()):
        raise HTTPException(401, 'Invalid signature')
    try:
        messages = parse_payload(bytes(raw), config)
    except (ValueError, ValidationError, RecursionError):
        raise HTTPException(400, 'Invalid payload') from None
    # Commit before acknowledgment; database failures propagate for provider retry.
    inserted = repo.store(messages)
    logger.info('whatsapp inbound accepted count=%d duplicates=%d', inserted, len(messages)-inserted)
    return {'accepted': inserted}


class Association(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    supplier_id: int = Field(gt=0)
    rfq_id: str = Field(min_length=1,max_length=100)


@router.get('/whatsapp/inbound', dependencies=[Depends(require_api_token),Depends(enabled)])
def review(response: Response, after: int = Query(0,ge=0),repo=Depends(get_repo)):
    response.headers['Cache-Control'] = 'no-store'
    return {'messages': repo.list(after)}


@router.patch('/whatsapp/inbound/{mid}', dependencies=[Depends(require_api_token),Depends(enabled)])
def associate(mid: int, body: Association, repo=Depends(get_repo)):
    try:
        repo.associate(mid,body.supplier_id,body.rfq_id)
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from None
    return {'associated': True}
