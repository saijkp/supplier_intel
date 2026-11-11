"""Authenticated thin HTTP layer. All mutations are explicit user actions."""
import json
import uuid
from typing import Literal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from api.auth import require_api_token
from outreach.service import OutreachService, stamp
from outreach.defaults import steps as default_steps

router = APIRouter(prefix='/outreach',tags=['Outreach'],dependencies=[Depends(require_api_token)])


def get_service():
    return OutreachService()


def call(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from None


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Import(Strict):
    rows: list[dict] = Field(max_length=2000)
    source: str = Field(min_length=1,max_length=300)
    source_url: str = Field(default='',max_length=2000)


class Step(Strict):
    channel: Literal['email','linkedin_task','visit_task','call_task']
    day_offset: int = Field(ge=0,le=365)
    subject: str = Field(default='',max_length=300)
    template: str = Field(min_length=1,max_length=10000)


class Sequence(Strict):
    title: str = Field(min_length=1,max_length=300)
    segment: Literal['architect','interior_designer','showroom_dealer','uk_brand','distributor','developer']
    steps: list[Step] | None = None


class Enrol(Strict):
    contact_ids: list[int] = Field(min_length=1,max_length=1000)
    mailbox: Literal['sai@ikeng.online','sj@ikeng.online']


class Approval(Strict):
    approved_by: Literal['Sai']
    legitimate_interest_note: str = Field(min_length=10,max_length=10000)


class Note(Strict):
    text: str = Field(min_length=1,max_length=20000)


class Reply(Note):
    external_id: str = Field(min_length=1,max_length=300)


class Reconcile(Note):
    confirmed_by: Literal['Sai']


@router.post('/events/{event_id}/reconcile')
def reconcile(event_id:int,body:Reconcile,s=Depends(get_service)):
    return call(s.reconcile,event_id,body.confirmed_by,body.text)


@router.post('/enrolments/{eid}/pause')
def pause(eid:int,s=Depends(get_service)):
    return call(s.pause,eid)


class Review(Strict):
    email_status: Literal['unreviewed','corporate_verified','invalid']
    project_hint: str | None = None
    project_hint_source: str | None = None


class Account(Strict):
    segment: Literal['architect','interior_designer','showroom_dealer','uk_brand','distributor','developer'] | None = None
    subscriber_type: Literal['corporate','sole_trader','partnership','unknown'] | None = None
    fit_notes: str | None = None
    priority: Literal['high','low'] | None = None
    owner: str | None = None
    flagged: bool | None = None
    flag_reason: str | None = None
    area: str | None = None


class Suppression(Strict):
    identifier: str = Field(min_length=3,max_length=300)
    reason: str = Field(min_length=1,max_length=1000)


class Visit(Strict):
    outcome: str = Field(min_length=1,max_length=10000)
    card: dict | None = None


class Project(Strict):
    enquiry_id: int


@router.post('/accounts/{aid}/enquiries')
def link_project(aid:int,body:Project,s=Depends(get_service)):
    return call(s.link_enquiry,aid,body.enquiry_id)


class Close(Strict):
    enquiry_id: int | None = None
    outcome: Literal['won','lost']
    close_note: str = Field(min_length=1,max_length=10000)
    won_value: float | None = None
    close_reason: str | None = None
    margin_gbp: float | None = None


class Job(Strict):
    limit: int = Field(default=10,ge=1,le=100)


class ManualTask(Strict):
    channel: Literal['linkedin_task','visit_task','call_task','needs Sai']
    text: str = Field(min_length=1,max_length=10000)
    due_at: str


@router.post('/enrolments/{eid}/tasks')
def manual_task(eid:int,body:ManualTask,s=Depends(get_service)):
    return call(s.manual_task,eid,**body.model_dump())


class RevealPreview(Strict):
    contact_ids: list[int] = Field(min_length=1,max_length=10)


class RevealApproval(Strict):
    confirmed_credit_cost: int = Field(ge=1)
    approved_by: Literal['Sai']


@router.post('/contacts/reveal/previews')
def reveal_preview(body:RevealPreview,s=Depends(get_service)):
    from outreach.apollo import ProspectContactService
    return call(ProspectContactService(s).preview_reveal,body.contact_ids)


@router.post('/contacts/reveal/{bid}/approve')
def reveal_approve(bid:str,body:RevealApproval,s=Depends(get_service)):
    from outreach.apollo import ProspectContactService
    return call(ProspectContactService(s).reveal,bid,body.confirmed_credit_cost,body.approved_by)


@router.post('/contacts/search/jobs',status_code=202)
def contact_search(body:Job,background:BackgroundTasks,s=Depends(get_service)):
    from outreach.apollo import ProspectContactService
    if body.limit>25:
        raise HTTPException(400,'Free search limit is 25 accounts')
    jid=uuid.uuid4().hex
    s.repo.execute('INSERT INTO outreach_jobs(id,status,created_at,updated_at) VALUES(?,?,?,?)',(jid,'pending',stamp(s.now()),stamp(s.now())))
    def run():
        try:
            result=ProspectContactService(s).search_pending(body.limit)
            status='completed'
        except Exception:
            result={'error':'Free contact search failed; no reveal performed'};status='failed'
        s.repo.execute('UPDATE outreach_jobs SET status=?,result=?,updated_at=? WHERE id=?',(status,json.dumps(result),stamp(s.now()),jid))
    background.add_task(run)
    return {'id':jid,'status':'pending','credit_cost':0,'email_reveal':False}


@router.get('/accounts')
def accounts(s=Depends(get_service)):
    return s.repo.rows('SELECT * FROM prospect_accounts ORDER BY flagged,priority,company')


@router.get('/accounts/{aid}')
def account(aid:int,s=Depends(get_service)):
    return call(s.account,aid)


@router.patch('/accounts/{aid}')
def update(aid:int,body:Account,s=Depends(get_service)):
    return call(s.update_account,aid,**body.model_dump(exclude_none=True))


@router.post('/accounts/{aid}/notes')
def note(aid:int,body:Note,s=Depends(get_service)):
    call(s.account,aid)
    return {'event_id':s.event(aid,'note',{'text':body.text})}


@router.post('/accounts/{aid}/stage')
def stage(aid:int,body:Note,s=Depends(get_service)):
    return call(s.stage,aid,body.text)


@router.post('/accounts/{aid}/visit')
def visit(aid:int,body:Visit,s=Depends(get_service)):
    return call(s.visit,aid,body.outcome,body.card)


@router.post('/accounts/{aid}/close')
def close(aid:int,body:Close,s=Depends(get_service)):
    return call(s.close,aid,**body.model_dump())


@router.post('/imports')
def imports(body:Import,s=Depends(get_service)):
    return call(s.import_rows,body.rows,body.source,body.source_url)


@router.patch('/contacts/{cid}')
def review(cid:int,body:Review,s=Depends(get_service)):
    return call(s.review_contact,cid,**body.model_dump())


@router.get('/sequences')
def sequences(s=Depends(get_service)):
    return [s.sequence(r['id']) for r in s.repo.rows('SELECT id FROM outreach_sequences ORDER BY id DESC')]


@router.post('/sequences')
def create_sequence(body:Sequence,s=Depends(get_service)):
    steps = [x.model_dump() for x in body.steps] if body.steps is not None else default_steps(body.segment)
    return call(s.save_sequence,body.title,body.segment,steps)


@router.put('/sequences/{sid}')
def edit_sequence(sid:int,body:Sequence,s=Depends(get_service)):
    steps = [x.model_dump() for x in body.steps] if body.steps is not None else default_steps(body.segment)
    return call(s.save_sequence,body.title,body.segment,steps,sid)


@router.post('/sequences/{sid}/enrol')
def enrol(sid:int,body:Enrol,s=Depends(get_service)):
    return call(s.enrol,sid,body.contact_ids,body.mailbox)


@router.post('/sequences/{sid}/approve')
def approve(sid:int,body:Approval,s=Depends(get_service)):
    return call(s.approve,sid,body.approved_by,body.legitimate_interest_note)


@router.get('/tasks')
def tasks(s=Depends(get_service)):
    return s.tasks()


@router.post('/tasks/{tid}/done')
def done(tid:int,body:Note,s=Depends(get_service)):
    return call(s.task_done,tid,body.text)


@router.post('/enrolments/{eid}/reply')
def reply(eid:int,body:Reply,s=Depends(get_service)):
    return call(s.receive,eid,body.text,body.external_id)


@router.post('/suppressions')
def suppress(body:Suppression,s=Depends(get_service)):
    call(s.suppress,body.identifier,body.reason)
    return {'suppressed':True}


@router.get('/suppressions')
def suppressions(s=Depends(get_service)):
    return s.repo.rows('SELECT * FROM suppression_list ORDER BY added_at DESC')


@router.get('/metrics')
def metrics(s=Depends(get_service)):
    return s.metrics()


def run_job(s,jid,limit):
    s.repo.execute("UPDATE outreach_jobs SET status='running',updated_at=? WHERE id=?",(stamp(s.now()),jid))
    try:
        result = s.run_pending(limit)
        status = 'failed' if result.get('failed') else 'completed'
    except Exception:
        status='failed'; result={'error':'Routine stopped; inspect mailbox configuration and uncertain sends'}
    s.repo.execute('UPDATE outreach_jobs SET status=?,result=?,updated_at=? WHERE id=?',(status,json.dumps(result),stamp(s.now()),jid))


@router.post('/jobs',status_code=202)
def job(body:Job,background:BackgroundTasks,s=Depends(get_service)):
    jid = uuid.uuid4().hex
    s.repo.execute('INSERT INTO outreach_jobs(id,status,created_at,updated_at) VALUES(?,?,?,?)',(jid,'pending',stamp(s.now()),stamp(s.now())))
    background.add_task(run_job,s,jid,body.limit)
    return {'id':jid,'status':'pending'}


@router.get('/jobs/{jid}')
def job_status(jid:str,s=Depends(get_service)):
    rows=s.repo.rows('SELECT * FROM outreach_jobs WHERE id=?',(jid,))
    if not rows:
        raise HTTPException(404,'Job not found')
    return rows[0]
