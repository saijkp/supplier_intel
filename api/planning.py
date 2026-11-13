"""Authenticated project endpoints; enrolment creates unapproved drafts only."""
from typing import Literal
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import Field
from api.auth import require_api_token
from api.outreach import Strict, call, get_service
from planning.service import PlanningService

router = APIRouter(prefix='/planning', tags=['Planning'], dependencies=[Depends(require_api_token)])


class Enrol(Strict):
    contact_id: int
    mailbox: Literal['sai@ikeng.online','sj@ikeng.online'] = 'sj@ikeng.online'
    role: Literal['architect','developer'] = 'architect'


class Sync(Strict):
    areas: list[str] = Field(min_length=1,max_length=50)
    days: int = Field(default=90,ge=1,le=180)
    statuses: list[Literal['validated','decided']] | None = None
    min_units: int = Field(default=4,ge=1,le=10000)


@router.get('/projects')
def projects(days: int = Query(default=7,ge=1,le=3650), s=Depends(get_service)):
    return PlanningService(s).projects(days)


@router.post('/projects/{pid}/enrol')
def enrol(pid: int, body: Enrol, s=Depends(get_service)):
    return call(PlanningService(s).enrol,pid,**body.model_dump())


@router.post('/sync', status_code=202)
def sync(body: Sync, background: BackgroundTasks, s=Depends(get_service)):
    service = PlanningService(s)
    run_id = service.start_run()
    background.add_task(service.run_background, run_id, **body.model_dump())
    return {'id': run_id, 'status': 'pending', 'status_url': f'/planning/runs/{run_id}'}


@router.get('/runs')
def runs(s=Depends(get_service)):
    PlanningService(s).expire_stalled()
    return s.repo.rows('SELECT * FROM planning_sync_runs ORDER BY id DESC LIMIT 20')


@router.get('/runs/{run_id}')
def run_status(run_id: int, s=Depends(get_service)):
    PlanningService(s).expire_stalled()
    rows = s.repo.rows('SELECT * FROM planning_sync_runs WHERE id=?', (run_id,))
    if not rows:
        from fastapi import HTTPException
        raise HTTPException(404, 'Planning run not found')
    return rows[0]


class Preview(Strict):
    contact_id: int


@router.post('/projects/{pid}/preview')
def preview(pid: int, body: Preview, s=Depends(get_service)):
    return call(PlanningService(s).preview, pid, body.contact_id)


class Identity(Strict):
    source_url: str = Field(min_length=1,max_length=2000)
    source_text: str = Field(min_length=1,max_length=100000)
    values: dict[str,str]


@router.post('/projects/{pid}/identity')
def identity(pid: int, body: Identity, s=Depends(get_service)):
    return call(PlanningService(s).import_identity, pid, **body.model_dump())


@router.post('/accounts/{aid}/contacts/search')
def search_contacts(aid: int, s=Depends(get_service)):
    return call(PlanningService(s).search_contacts, aid)
