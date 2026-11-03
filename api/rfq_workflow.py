"""Authenticated controls for the linked outreach workflow."""
import uuid
import hashlib
import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from api.auth import require_api_token
from api.rfq import get_rfq_repo, load
from rfq.workspace_workflow import WorkspaceWorkflow
from storage.repository import SupplierRepository

router=APIRouter(prefix='/rfqs',tags=['RFQ outreach workflow'],dependencies=[Depends(require_api_token)])


class LinkRequest(BaseModel):
    campaign_id:int=Field(gt=0)
    confirm_specification_match:bool=False


class DraftRequest(BaseModel):
    supplier_ids:list[PositiveInt]=Field(default_factory=list,max_length=25)
    query:str=Field(default='',max_length=300)


class PolicyRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    poll_enabled:bool=False
    auto_send:bool=False
    interval_minutes:int=Field(default=15,ge=5,le=1440)
    followup_hours:int=Field(default=24,ge=1,le=168)
    max_rounds:int=Field(default=3,ge=1,le=5)
    days:int=Field(default=14,ge=1,le=30)
    approve_scope:bool=False


class ReplyRequest(BaseModel):
    text:str=Field(min_length=1,max_length=50000)


class ChatImportRequest(BaseModel):
    text:str=Field(min_length=1,max_length=2000000)
    supplier_sender:str=Field(min_length=1,max_length=200)


def get_workflow(repo=Depends(get_rfq_repo)):
    return WorkspaceWorkflow(repo.db_path)


def call(fn,*args,**kwargs):
    try: return fn(*args,**kwargs)
    except ValueError as exc: raise HTTPException(422,str(exc)) from None


@router.get('/outreach/campaigns')
def campaigns(workflow=Depends(get_workflow)):
    return {'campaigns':workflow.outreach.repo.rows('campaigns'),'readiness':workflow.outreach.status()}


@router.get('/{rid}/outreach')
def view(rid:str,workflow=Depends(get_workflow)):
    return call(workflow.view,rid)


@router.post('/{rid}/outreach/link')
def link(rid:str,body:LinkRequest,workflow=Depends(get_workflow)):
    return call(workflow.link,rid,body.campaign_id,body.confirm_specification_match)


@router.post('/{rid}/outreach/create')
def create(rid:str,body:DraftRequest,workflow=Depends(get_workflow)):
    return call(workflow.create,rid,body.supplier_ids,body.query)


@router.post('/{rid}/outreach/policy')
def policy(rid:str,body:PolicyRequest,workflow=Depends(get_workflow)):
    return call(workflow.set_policy,rid,**body.model_dump())


@router.post('/{rid}/outreach/pause')
def pause(rid:str,workflow=Depends(get_workflow)):
    if not workflow.repo.get(rid): raise HTTPException(422,'Link an outreach campaign first')
    workflow.repo.policy(rid,poll_enabled=0,auto_send=0)
    return call(workflow.view,rid)


def run_sync(job_id,rid,db_path):
    repo=SupplierRepository(db_path)
    repo.mark_pipeline_job_running(job_id)
    result=WorkspaceWorkflow(db_path).sync(rid)
    if result.get('error'): repo.mark_pipeline_job_failed(job_id,error=result['error'])
    else: repo.mark_pipeline_job_completed(job_id,stats=result)


@router.post('/{rid}/outreach/sync',status_code=202)
def sync(rid:str,tasks:BackgroundTasks,workflow=Depends(get_workflow)):
    if not workflow.repo.get(rid): raise HTTPException(422,'Link an outreach campaign first')
    job_id=str(uuid.uuid4())
    SupplierRepository(workflow.repo.db_path).create_pipeline_job(job_id,query='[RFQ] collect replies and validate',options={'rfq_id':rid})
    tasks.add_task(run_sync,job_id,rid,workflow.repo.db_path)
    return {'job_id':job_id,'status':'queued'}


@router.post('/{rid}/outreach/messages/{mid}/reply')
def reply(rid:str,mid:int,body:ReplyRequest,workflow=Depends(get_workflow)):
    policy=workflow.repo.get(rid)
    message=call(workflow.outreach.repo.get,'messages',mid)
    if not policy or message['campaign_id']!=policy['campaign_id']: raise HTTPException(422,'Message does not belong to this RFQ')
    call(workflow.outreach.record_manual_reply,mid,body.text)
    return call(workflow.sync,rid,poll=False)


@router.post('/{rid}/outreach/messages/{mid}/whatsapp-import')
def import_chat(rid:str,mid:int,body:ChatImportRequest,workflow=Depends(get_workflow)):
    from rfq.whatsapp_import import supplier_messages
    link=workflow.repo.get(rid); message=call(workflow.outreach.repo.get,'messages',mid)
    if not link or message['campaign_id']!=link['campaign_id']: raise HTTPException(422,'Message does not belong to this RFQ')
    messages=call(supplier_messages,body.text,body.supplier_sender)
    if any(len(entry['text'])>50000 for entry in messages):
        raise HTTPException(422,'Select supplier messages shorter than 50,000 characters')
    count=0
    for entry in messages:
        # Stable IDs deduplicate a reimport without ever storing buyer chat messages.
        fingerprint='whatsapp:'+hashlib.sha256((str(mid)+'\0'+entry['sender']+'\0'+entry['timestamp']+'\0'+entry['text']).encode()).hexdigest()
        media=bool(re.search(r'<Media omitted>|image omitted|audio omitted|省略的媒体|媒体已省略',entry['text'],re.I))
        reply=workflow.outreach._reply(message,entry['text'],source='whatsapp_import',external_message_id=fingerprint,
                   attachments=[{'filename':'WhatsApp media not included','read_status':'needs_manual_review'}] if media else [])
        if not reply.get('duplicate'):
            workflow.outreach.repo.update('replies',reply['id'],channel='whatsapp')
            count+=1
    result=call(workflow.sync,rid,poll=False)
    return {'imported':count,'duplicate_or_skipped':len(messages)-count,'workflow':result}


@router.post('/{rid}/outreach/messages/{mid}/send',status_code=202)
def send(rid:str,mid:int,tasks:BackgroundTasks,workflow=Depends(get_workflow)):
    policy=workflow.repo.get(rid)
    message=call(workflow.outreach.repo.get,'messages',mid)
    if not policy or message['campaign_id']!=policy['campaign_id'] or message['channel']!='email':
        raise HTTPException(422,'Email message does not belong to this RFQ')
    if not workflow.outreach.status()['ready']: raise HTTPException(422,'Sending is disabled or mailbox / buyer identity configuration is incomplete')
    if message['status'] not in ('draft','approved'): raise HTTPException(422,'Only an unsent draft can be approved and sent')
    workflow.outreach.approve(policy['campaign_id'],[mid])
    workflow.repo.approve_manual(mid)
    job_id=str(uuid.uuid4())
    SupplierRepository(workflow.repo.db_path).create_pipeline_job(job_id,query='[RFQ] send approved message',options={'message_id':mid})
    tasks.add_task(run_send,job_id,policy['campaign_id'],mid,workflow.repo.db_path)
    return {'job_id':job_id,'status':'queued'}


def run_send(job_id,campaign_id,mid,db_path):
    repo=SupplierRepository(db_path); repo.mark_pipeline_job_running(job_id)
    try:
        result=WorkspaceWorkflow(db_path).outreach.send_approved(campaign_id,message_ids=[mid])
        repo.mark_pipeline_job_completed(job_id,stats=result)
    except Exception:
        repo.mark_pipeline_job_failed(job_id,error='Sending failed; review message delivery state before retrying')
