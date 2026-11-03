"""A supplier reply moves from outreach into the comparison through the UI."""
from pathlib import Path
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright, expect

import api.auth
from api.rfq import router, get_rfq_repo
from api.rfq_workflow import router as workflow_router
from config import settings
from storage.database import initialise_schema
from storage.quote_workspace_repository import QuoteWorkspaceRepository
from storage.repository import SupplierRepository
from rfq.rfq_service import RFQService
from storage.rfq_repository import RFQRepository


@pytest.mark.slow
def test_link_outreach_and_check_partial_replies(tmp_path,monkeypatch):
    path=tmp_path/'browser.db';initialise_schema(path)
    monkeypatch.setattr(api.auth,'API_ACCESS_TOKEN','workflow-browser')
    monkeypatch.setattr(settings,'OPENAI_API_KEY',None)
    monkeypatch.setattr(settings,'RFQ_SEND_ENABLED',False)
    monkeypatch.setattr(settings,'RFQ_IMAP_HOST','')
    sid=SupplierRepository(path).create_golden_record({'canonical_name':'Test factory','primary_email':'sales@test.test'})
    service=RFQService(repo=RFQRepository(path))
    campaign=service.create_campaign(title='Supplier replies',product='Lights',quantity='100',unit='pcs')
    service.draft_messages(campaign['id'],supplier_ids=[sid])
    mid=service.repo.rows('messages')[0]['id'];service.repo.update('messages',mid,status='sent',sent_at='2000-01-01 00:00:00')
    app=FastAPI();app.include_router(workflow_router);app.include_router(router)
    app.dependency_overrides[get_rfq_repo]=lambda:QuoteWorkspaceRepository(path)
    app.mount('/workspace',StaticFiles(directory=Path(__file__).resolve().parents[1]/'frontend',html=True))
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        with sync_playwright() as p:
            browser=p.chromium.launch();page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}/workspace/rfq.html');page.locator('#token').fill('workflow-browser');page.get_by_role('button',name='Connect',exact=True).click()
            page.get_by_role('button',name='+ New RFQ').click();page.locator('#title').fill('LED RFQ');page.locator('#quantity').fill('100');page.locator('#specs').fill('material = aluminium');page.get_by_role('button',name='Create RFQ',exact=True).click()
            page.locator('#outreachCampaign').select_option(str(campaign['id']));page.locator('#confirmCampaign').check();page.get_by_role('button',name='Link campaign',exact=True).click()
            expect(page.locator('#outreachPanel')).to_contain_text('0/1 supplier quotes complete')
            reply=page.get_by_role('textbox',name='Supplier reply for Test factory').first
            reply.fill('unit_price: 18\ncurrency: USD\nquoted_quantity: 100\nincoterm: FOB\nnamed_place: Shenzhen\nproduction_lead_days: 25\npayment_terms: 30/70\nvalid_until: 2099-12-31\nmaterial: aluminium')
            page.get_by_role('button',name='Record reply & check').first.click()
            expect(page.locator('#comparison')).to_contain_text('Not supplied')
            expect(page.locator('.draft')).to_have_count(1)
            assert '最小起订量' in page.locator('.draft').input_value()
            page.get_by_role('textbox',name='Supplier reply for Test factory').first.fill('moq: 50');page.get_by_role('button',name='Record reply & check').first.click()
            expect(page.locator('.badge.comparable')).to_have_count(1)
            expect(page.locator('#outreachPanel')).to_contain_text('1/1 supplier quotes complete')
            expect(page.locator('#comparison tr').first.locator('th')).to_have_count(2)
            expect(page.locator('.draft')).to_have_count(0)
            page.reload();page.locator('.rfq-item').click();expect(page.locator('.badge.comparable')).to_have_count(1)
            assert errors==[];browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
