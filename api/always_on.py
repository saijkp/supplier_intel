"""Authenticated controls and read-only outreach health; no secrets returned."""
import uuid
from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel, ConfigDict
from api.auth import require_api_token
from api.outreach import get_service, call
from outreach.always_on import AlwaysOnService, project_steps
from outreach.service import stamp

router=APIRouter(dependencies=[Depends(require_api_token)],tags=['Always-on outreach'])


def get_loop(s=Depends(get_service)):
    return AlwaysOnService(s)


class Approval(BaseModel):
    model_config=ConfigDict(extra='forbid')
    approved_by: str
    note: str


class Pause(BaseModel):
    paused: bool


class Seed(BaseModel):
    mailbox: str
    seed: str
    placement: str
    confirmed_by: str


@router.get('/health/outreach')
def health(s=Depends(get_loop)):
    return s.health()


@router.get('/outreach/always-on/status')
def status(s=Depends(get_loop)):
    return {'trial_decision_makers':s.trial_decision_makers(), 'health':s.health(),'daily':s.summary(),'weekly':s.summary(7),
            'runs':s.repo.rows('SELECT * FROM outreach_daily_runs ORDER BY day DESC LIMIT 7'),
            'queue':s.repo.rows('SELECT * FROM outreach_project_queue ORDER BY attempted_at LIMIT 100'),
            'identity_queue':s.repo.rows("SELECT key,payload FROM outreach_runtime WHERE key LIKE 'project:%' OR key LIKE 'identity:%' ORDER BY key LIMIT 100"),
            'template':s.state('template_approval'),'template_steps':project_steps()}


@router.get('/outreach/always-on/preview')
def preview(s=Depends(get_loop)):
    return {'dry_run':s.dry_run(),'emails':s.preview(10)}


@router.post('/outreach/always-on/template/approve')
def approve(body:Approval,s=Depends(get_loop)):
    return call(s.approve_template,body.approved_by,body.note)


@router.post('/outreach/always-on/pause')
def pause(body:Pause,s=Depends(get_loop)):
    s.put('pause',body.paused)
    return {'paused':s.paused()}


@router.post('/outreach/always-on/sequences/{sid}/pause')
def sequence_pause(sid:int,body:Pause,s=Depends(get_loop)):
    call(s.repo.get,'outreach_sequences',sid)
    s.put('sequence_pause:'+str(sid),body.paused)
    return {'paused':body.paused}


@router.post('/outreach/always-on/seeds/result')
def seeds(body:Seed,s=Depends(get_loop)):
    call(s.seed_result,**body.model_dump())
    return {'recorded':True}


@router.post('/outreach/always-on/jobs',status_code=202)
def run(background:BackgroundTasks,s=Depends(get_loop)):
    from storage.repository import SupplierRepository
    repo=SupplierRepository(s.repo.db_path)
    jid=uuid.uuid4().hex
    repo.create_pipeline_job(job_id=jid,query='Daily project-led outreach',options={'kind':'always_on_outreach'})
    def execute():
        repo.mark_pipeline_job_running(jid)
        result=s.tick()
        if result.get('failed'):
            repo.mark_pipeline_job_failed(jid,error='Daily outreach failed; inspect health endpoint')
        else:
            repo.mark_pipeline_job_completed(jid,stats=result)
    background.add_task(execute)
    return {'id':jid,'status':'pending'}
