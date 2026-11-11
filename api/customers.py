"""Customer profiles and enquiry follow-ups (IK Eng's buy side). Thin wrapper over CustomerService."""
from typing import List, Literal, Optional  # noqa: F401

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_api_token
from customers.customer_service import CustomerService

router = APIRouter(prefix='/customers', tags=['Customers'], dependencies=[Depends(require_api_token)])


def get_customer_service() -> CustomerService:
    return CustomerService()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        status = 404 if 'not found' in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from None


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Optional[str] = Field(default=None, max_length=300)
    company: Optional[str] = Field(default=None, max_length=300)
    email: Optional[str] = Field(default=None, max_length=300)
    phone: Optional[str] = Field(default=None, max_length=60)
    whatsapp: Optional[str] = Field(default=None, max_length=60)
    location: Optional[str] = Field(default=None, max_length=300)
    source: Optional[str] = Field(default=None, max_length=300)
    notes: Optional[str] = Field(default=None, max_length=20000)


class EnquiryIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Optional[str] = Field(default=None, max_length=300)
    waiting_on: Optional[str] = None
    qualification: Optional[dict] = None
    requirements: Optional[str] = Field(default=None, max_length=20000)
    status: Optional[str] = Field(default=None, max_length=60)
    next_action: Optional[str] = Field(default=None, max_length=5000)
    next_action_due: Optional[str] = Field(default=None, max_length=30)
    blocker: Optional[str] = Field(default=None, max_length=5000)
    estimated_value: Optional[str] = Field(default=None, max_length=300)
    owner: Optional[str] = Field(default=None, max_length=120)
    summary: Optional[str] = Field(default=None, max_length=20000)
    last_customer_contact_at: Optional[str] = Field(default=None, max_length=30)
    last_our_contact_at: Optional[str] = Field(default=None, max_length=30)


class EnquiryCreate(EnquiryIn):
    reference: Optional[str] = Field(default=None, max_length=20)
    source_ref: Optional[str] = Field(default=None, max_length=300)


class ActivityIn(BaseModel):
    kind: str = Field(max_length=30)
    summary: str = Field(min_length=1, max_length=20000)
    occurred_at: Optional[str] = Field(default=None, max_length=30)


class FollowupIn(BaseModel):
    channel: Literal['email', 'whatsapp'] = 'email'
    subject: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = Field(default=None, max_length=20000)


class MessageEdit(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = Field(default=None, max_length=20000)


class LinkCampaign(BaseModel):
    campaign_id: int = Field(gt=0)


class PollIn(BaseModel):
    since_days: int = Field(default=14, ge=1, le=90)
    limit: int = Field(default=50, ge=1, le=200)


def _set(model):
    return model.model_dump(exclude_unset=True)


@router.get('/status')
def customer_status(service: CustomerService = Depends(get_customer_service)):
    return service.status()


@router.get('')
def list_customers(q: Optional[str] = None, service: CustomerService = Depends(get_customer_service)):
    return service.list_customers(q)


@router.post('')
def create_customer(body: CustomerIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.create_customer, **_set(body))


@router.get('/enquiries')
def list_enquiries(status: Optional[str] = None, due_only: bool = False, include_closed: bool = False,
                   service: CustomerService = Depends(get_customer_service)):
    return service.list_enquiries(status=status, due_only=due_only, include_closed=include_closed)


@router.get('/enquiries/{enquiry_id}')
def get_enquiry(enquiry_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.get_enquiry, enquiry_id)


@router.patch('/enquiries/{enquiry_id}')
def update_enquiry(enquiry_id: int, body: EnquiryIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.update_enquiry, enquiry_id, **_set(body))


@router.post('/enquiries/{enquiry_id}/activities')
def add_activity(enquiry_id: int, body: ActivityIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.log, enquiry_id, body.kind, body.summary, occurred_at=body.occurred_at)


@router.post('/enquiries/{enquiry_id}/campaigns')
def link_campaign(enquiry_id: int, body: LinkCampaign, service: CustomerService = Depends(get_customer_service)):
    return _call(service.link_campaign, enquiry_id, body.campaign_id)


@router.post('/enquiries/{enquiry_id}/followups')
def draft_followup(enquiry_id: int, body: FollowupIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.draft_followup, enquiry_id, channel=body.channel, body=body.body, subject=body.subject)


@router.patch('/messages/{message_id}')
def edit_message(message_id: int, body: MessageEdit, service: CustomerService = Depends(get_customer_service)):
    return _call(service.edit_message, message_id, subject=body.subject, body=body.body)


@router.post('/messages/{message_id}/approve')
def approve_message(message_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.approve, message_id)


@router.post('/messages/{message_id}/send')
def send_message(message_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.send, message_id)


@router.post('/messages/{message_id}/mark-sent')
def mark_sent(message_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.mark_sent, message_id)


@router.post('/messages/{message_id}/cancel')
def cancel_message(message_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.cancel, message_id)


@router.post('/poll')
def poll_inbox(body: PollIn = PollIn(), service: CustomerService = Depends(get_customer_service)):
    try:
        return service.poll_inbox(since_days=body.since_days, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


class LeadConvert(BaseModel):
    name: Optional[str] = Field(default=None, max_length=300)
    title: Optional[str] = Field(default=None, max_length=300)


@router.get('/leads')
def list_leads(status: Literal['new', 'added', 'dismissed'] = 'new',
               service: CustomerService = Depends(get_customer_service)):
    return service.list_leads(status)


@router.post('/leads/{lead_id}/convert')
def convert_lead(lead_id: int, body: LeadConvert = LeadConvert(),
                 service: CustomerService = Depends(get_customer_service)):
    return _call(service.convert_lead, lead_id, name=body.name, title=body.title)


class LeadReply(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


@router.post('/leads/{lead_id}/reply')
def draft_lead_reply(lead_id: int, body: LeadReply, service: CustomerService = Depends(get_customer_service)):
    return _call(service.draft_lead_reply, lead_id, body.body)


@router.post('/leads/{lead_id}/dismiss')
def dismiss_lead(lead_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.dismiss_lead, lead_id)


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: Optional[float] = None
    currency: str = 'GBP'
    summary: Optional[str] = None
    sent_at: Optional[str] = None
    valid_until: Optional[str] = None
    status: str = 'draft'


class CloseIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    outcome: str
    close_reason: Optional[str] = None
    close_note: Optional[str] = None
    won_value: Optional[float] = None


class ReopenIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: str = 'qualifying'


@router.get('/today')
def today(service: CustomerService = Depends(get_customer_service)):
    return service.today()


@router.get('/pipeline')
def pipeline(service: CustomerService = Depends(get_customer_service)):
    return service.pipeline()


@router.post('/enquiries/{enquiry_id}/quotes')
def add_quote(enquiry_id: int, body: QuoteIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.add_quote, enquiry_id, **_set(body))


@router.patch('/enquiries/{enquiry_id}/quotes/{quote_id}')
def update_quote(enquiry_id: int, quote_id: int, body: QuoteIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.update_quote, enquiry_id, quote_id, **_set(body))


@router.post('/enquiries/{enquiry_id}/close')
def close(enquiry_id: int, body: CloseIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.close_enquiry, enquiry_id, **_set(body))


@router.post('/enquiries/{enquiry_id}/reopen')
def reopen(enquiry_id: int, body: ReopenIn = ReopenIn(), service: CustomerService = Depends(get_customer_service)):
    return _call(service.reopen_enquiry, enquiry_id, **_set(body))


class ActionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['done', 'snooze', 'waiting']
    due: Optional[str] = None


@router.get('/next-actions')
def next_actions(service: CustomerService = Depends(get_customer_service)):
    from customers.next_actions import next_actions as queue
    return queue(service)


@router.post('/enquiries/{enquiry_id}/action')
def enquiry_action(enquiry_id: int, body: ActionIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.act, enquiry_id, body.action, body.due)


class ComposeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Optional[str] = Field(default=None, max_length=300)
    enquiry: Optional[int] = None
    lead: Optional[int] = None
    body: str = Field(min_length=1, max_length=20000)
    subject: Optional[str] = Field(default=None, max_length=300)


@router.post('/compose')
def compose(body: ComposeIn, service: CustomerService = Depends(get_customer_service)):
    from config import settings as cfg
    if not body.body.strip():
        raise HTTPException(400, 'Write a reply first')
    if bool(body.enquiry) == bool(body.lead):
        raise HTTPException(400, 'Choose one enquiry or inbound email')
    if body.lead:
        lead = _call(service.repo.get, 'customer_inbox_leads', body.lead)
        if lead['status'] == 'new' and not lead.get('from_name'):
            if not (body.name or '').strip():
                raise HTTPException(400, 'Enter the customer name from their message')
            _call(service.convert_lead, body.lead, name=body.name)
        message = _call(service.draft_lead_reply, body.lead, body.body)
    else:
        message = _call(service.draft_followup, body.enquiry, body=body.body, subject=body.subject)
    service.repo.update('customer_messages', message['id'], mailbox=cfg.CUSTOMER_ZOHO_ADDRESS.lower())
    return service.repo.get('customer_messages', message['id'])


@router.post('/messages/{message_id}/send-now')
def send_now(message_id: int, service: CustomerService = Depends(get_customer_service)):
    # The explicit Send click approves exactly this stored draft. No new send plumbing.
    message = _call(service.repo.get, 'customer_messages', message_id)
    from config import settings as cfg
    if message['channel'] != 'email' or message.get('mailbox') != cfg.CUSTOMER_ZOHO_ADDRESS.lower():
        raise HTTPException(400, 'Use the customer mailbox for this action')
    if message['status'] == 'draft':
        _call(service.approve, message_id)
    result = _call(service.send, message_id)
    _call(service.act, message['enquiry_id'], 'waiting')
    return result


@router.get('/{customer_id}')
def get_customer(customer_id: int, service: CustomerService = Depends(get_customer_service)):
    return _call(service.get_customer, customer_id)


@router.patch('/{customer_id}')
def update_customer(customer_id: int, body: CustomerIn, service: CustomerService = Depends(get_customer_service)):
    return _call(service.update_customer, customer_id, **_set(body))


@router.post('/{customer_id}/enquiries')
def create_enquiry(customer_id: int, body: EnquiryCreate, service: CustomerService = Depends(get_customer_service)):
    values = _set(body)
    return _call(service.create_enquiry, customer_id, reference=values.pop('reference', None),
                 source_ref=values.pop('source_ref', None), **values)
