"""Authenticated project endpoints; enrolment creates unapproved drafts only."""
from typing import Literal
from fastapi import APIRouter, Depends, Query
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


@router.get('/projects')
def projects(days: int = Query(default=7,ge=1,le=3650), s=Depends(get_service)):
    return PlanningService(s).projects(days)


@router.post('/projects/{pid}/enrol')
def enrol(pid: int, body: Enrol, s=Depends(get_service)):
    return call(PlanningService(s).enrol,pid,**body.model_dump())


@router.post('/sync')
def sync(body: Sync, s=Depends(get_service)):
    return call(PlanningService(s).sync_recent,body.areas,body.days)


@router.get('/runs')
def runs(s=Depends(get_service)):
    return s.repo.rows('SELECT * FROM planning_sync_runs ORDER BY id DESC LIMIT 20')
