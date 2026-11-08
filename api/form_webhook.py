"""Public Netlify intake; authenticates raw bytes using Netlify's HS256 JWS."""
import base64
import binascii
import hashlib
import hmac
import json
import math
import time
from collections import OrderedDict
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request

from api.customers import get_customer_service
from config import settings as cfg
from customers.customer_service import CustomerService
from customers.form_parser import netlify_data, parse_netlify_form

router = APIRouter(prefix='/customers', tags=['Customers'])
MAX_BODY = 64 * 1024
RATE_LIMIT = 60
RATE_WINDOW = 60
_buckets = OrderedDict()
_rate_lock = Lock()


def _rate_limit(ip):
    # In-memory, per process; bounded memory. Use the peer IP, never a caller's
    # arbitrary X-Forwarded-For. Railway/proxy peer grouping can share this cap.
    now = time.monotonic()
    with _rate_lock:
        while _buckets and next(iter(_buckets.values()))[0] <= now - RATE_WINDOW:
            _buckets.popitem(last=False)
        start, count = _buckets.get(ip, (now, 0))
        if count >= RATE_LIMIT:
            raise HTTPException(429, 'Webhook rate limit exceeded', headers={'Retry-After': '60'})
        if len(_buckets) >= 4096 and ip not in _buckets:
            raise HTTPException(429, 'Webhook rate limit exceeded')
        _buckets[ip] = (start, count + 1)


def _decode(segment):
    return base64.b64decode(segment + '=' * (-len(segment) % 4), altchars=b'-_', validate=True)


def verify_signature(token, body, secret):
    try:
        head, claims, signature = token.split('.')
        header = json.loads(_decode(head))
        if header.get('alg') != 'HS256' or header.get('crit') or header.get('b64') is False:
            return False
        expected = hmac.new(secret.encode(), (head + '.' + claims).encode('ascii'), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            return False
        payload = json.loads(_decode(claims))
        if payload.get('iss') != 'netlify':
            return False
        # Netlify documents iss/hash only, not a mandatory expiry. Honor temporal
        # claims when present; do not reject its normal non-expiring signatures.
        for key in ('exp', 'nbf'):
            if key in payload:
                value = payload[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return False
                if (key == 'exp' and value <= time.time()) or (key == 'nbf' and value > time.time()):
                    return False
        digest = payload.get('sha256')
        return isinstance(digest, str) and hmac.compare_digest(
            digest.encode('utf-8'), hashlib.sha256(body).hexdigest().encode('ascii'))
    except (ValueError, TypeError, AttributeError, UnicodeError, binascii.Error, RecursionError, OverflowError):
        return False


@router.post('/form-webhook')
async def form_webhook(request: Request, service: CustomerService = Depends(get_customer_service)):
    if not cfg.NETLIFY_FORM_WEBHOOK_SECRET:
        raise HTTPException(503, 'NETLIFY_FORM_WEBHOOK_SECRET is not configured')
    _rate_limit(request.client.host if request.client else 'unknown')
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, 'Webhook body exceeds 64 KB')
        body.extend(chunk)
    raw = bytes(body)
    if not verify_signature(request.headers.get('x-webhook-signature', ''), raw, cfg.NETLIFY_FORM_WEBHOOK_SECRET):
        raise HTTPException(401, 'Invalid or missing Netlify signature')
    try:
        text = raw.decode('utf-8')
        payload = json.loads(text)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, 'Invalid webhook JSON') from None
    if not isinstance(payload, dict):
        raise HTTPException(400, 'Webhook must be a submission object')
    # Outgoing form notifications contain the submission itself, not an event
    # envelope. The configured hook selects submission_created. If an event is
    # supplied by Netlify, enforce it as well.
    event = request.headers.get('x-netlify-event') or payload.get('event')
    if event and event != 'submission_created':
        return {'status': 'ignored'}
    data = netlify_data(payload)
    form_name = payload.get('form_name') or data.get('form-name')
    if not isinstance(form_name, str) or form_name not in cfg.NETLIFY_FORM_NAMES or data.get('bot-field'):
        return {'status': 'ignored'}
    submission_id = payload.get('id')
    if not isinstance(submission_id, str) or not submission_id.strip() or len(submission_id) > 200:
        raise HTTPException(400, 'Missing or invalid submission id')
    if not any((isinstance(payload.get('data'), dict),
                isinstance(payload.get('human_fields'), dict),
                isinstance(payload.get('ordered_human_fields'), list))):
        raise HTTPException(400, 'Missing submission fields')
    result = service.store_form_submission(
        parse_netlify_form(payload), text, 'cust:netlify:' + submission_id,
        'Form submission from ' + form_name + ' form:')
    return {'status': result}
