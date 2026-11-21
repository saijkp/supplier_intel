"""Public chat boundary; origin allowlist is independent of administrative CORS."""
import os
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict
from api.auth import require_api_token
from chatbot.service import ChatbotService

router=APIRouter(prefix='/chat',tags=['Website chat'])

def get_chat_service():
    return ChatbotService()

async def origin(request: Request):
    allowed=[x.strip() for x in (os.getenv('CHATBOT_ALLOWED_ORIGINS') or '').split(',') if x.strip()]
    if request.headers.get('origin') not in allowed:
        raise HTTPException(403,'Website origin is not allowed')

class Start(BaseModel):
    model_config=ConfigDict(extra='forbid')
    honeypot: str=Field(default='',max_length=200)
class Message(Start):
    text: str=Field(min_length=1,max_length=2000)
class Registration(Start):
    name: str=Field(max_length=200)
    mobile: str=Field(max_length=60)
    email: str=Field(max_length=254)
    postcode: str=Field(max_length=12)
    address: str=Field(default='',max_length=500)
    consent: bool
class Styles(BaseModel):
    tags: list[str]=Field(max_length=8)

def call(fn,*args):
    try: return fn(*args)
    except ValueError as exc: raise HTTPException(400,str(exc)) from None

@router.post('/sessions',dependencies=[Depends(origin)])
def start(body:Start,request:Request,service=Depends(get_chat_service)):
    return call(service.create,request.client.host if request.client else 'unknown',body.honeypot)

@router.post('/sessions/{sid}/messages',dependencies=[Depends(origin)])
def message(sid:str,body:Message,service=Depends(get_chat_service)):
    return call(service.message,sid,body.text,body.honeypot)

@router.post('/sessions/{sid}/register',dependencies=[Depends(origin)])
def register(sid:str,body:Registration,service=Depends(get_chat_service)):
    return call(service.register,sid,body.model_dump())

@router.post('/sessions/{sid}/styles',dependencies=[Depends(origin)])
def styles(sid:str,body:Styles,service=Depends(get_chat_service)):
    return call(service.styles,sid,body.tags)

@router.post('/sessions/{sid}/files',dependencies=[Depends(origin)])
async def upload(sid:str,file:UploadFile=File(...),service=Depends(get_chat_service)):
    try:
        data=await file.read(15*1024*1024+1)
        return call(service.upload,sid,file.filename or '',data)
    finally:
        await file.close()

@router.get('/sessions',dependencies=[Depends(require_api_token)])
def review(since:float=0,service=Depends(get_chat_service)):
    return service.review(since)

@router.get('/files/{fid}',dependencies=[Depends(require_api_token)])
def download(fid:str,service=Depends(get_chat_service)):
    import re
    if not re.fullmatch('[a-f0-9]{32}',fid) or not (service.artifacts/fid).is_file():
        raise HTTPException(404,'File not found')
    return FileResponse(service.artifacts/fid,media_type='application/octet-stream',filename=fid,
        headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
