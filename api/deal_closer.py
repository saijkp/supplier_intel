"""Authenticated review endpoints; no customer/supplier send routes."""
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from api.auth import require_api_token
from api.customers import get_customer_service, _call
from customers.deal_closer import DealCloserService

router=APIRouter(tags=['Deal closer'],dependencies=[Depends(require_api_token)])

def get_service(customers=Depends(get_customer_service)):
    return DealCloserService(customers)

class PromiseIn(BaseModel):
    text: str = Field(min_length=1,max_length=5000)
    due_at: str
    made_on: str | None = None
    channel: Literal['email','whatsapp','call','meeting','manual'] = 'manual'

class PromiseUpdate(BaseModel):
    status: Literal['open','kept','missed','renegotiated'] | None = None
    confirmed: bool | None = None

class ConvertIn(BaseModel):
    name: str | None = None
    title: str | None = None

@router.get('/deal-closer/summary')
def summary(service=Depends(get_service)):
    return service.summary()

@router.get('/health/deal-closer')
def health(service=Depends(get_service)):
    return service.health()

@router.post('/deal-closer/digest')
def digest(service=Depends(get_service)):
    # Repeated clicks in a minute cannot duplicate delivery.
    return service.digest('manual '+service.clock().strftime('%Y-%m-%d %H:%M'))

@router.post('/enquiries/{enquiry_id}/promises')
def add_promise(enquiry_id:int,body:PromiseIn,service=Depends(get_service)):
    return _call(service.add_promise,enquiry_id,**body.model_dump())

@router.patch('/promises/{promise_id}')
def update_promise(promise_id:int,body:PromiseUpdate,service=Depends(get_service)):
    return _call(service.update_promise,promise_id,**body.model_dump())

@router.get('/deal-closer/triage')
def triage(service=Depends(get_service)):
    return service.customers.list_leads()

@router.post('/deal-closer/triage/{lead_id}/convert')
def convert(lead_id:int,body:ConvertIn=ConvertIn(),service=Depends(get_service)):
    return _call(service.customers.convert_lead,lead_id,**body.model_dump())

@router.post('/deal-closer/triage/{lead_id}/dismiss')
def dismiss(lead_id:int,service=Depends(get_service)):
    return _call(service.customers.dismiss_lead,lead_id)
