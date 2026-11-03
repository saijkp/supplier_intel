"""Exercise the clickable dashboard against real HTTP endpoints and SQLite."""
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
from storage.database import initialise_schema
from storage.quote_workspace_repository import QuoteWorkspaceRepository


@pytest.mark.slow
def test_create_compare_clarify_and_reload(tmp_path, monkeypatch):
    path = tmp_path / 'browser.db'
    initialise_schema(path)
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', 'browser-test')
    app = FastAPI()
    app.include_router(workflow_router)
    app.include_router(router)
    app.dependency_overrides[get_rfq_repo] = lambda: QuoteWorkspaceRepository(path)
    app.mount('/workspace', StaticFiles(directory=Path(__file__).resolve().parents[1] / 'frontend', html=True))
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{port}/workspace/rfq.html')
            page.locator('#token').fill('browser-test')
            page.get_by_role('button', name='Connect', exact=True).click()
            expect(page.locator('#message')).to_contain_text('Connected')
            page.get_by_role('button', name='+ New RFQ').click()
            page.locator('#title').fill('Browser RFQ <script>alert(1)</script>')
            page.locator('#quantity').fill('100')
            page.locator('#specs').fill('material = aluminium')
            page.get_by_role('button', name='Create RFQ', exact=True).click()
            expect(page.locator('#rfqTitle')).to_contain_text('<script>')
            for name, currency, material, missing_moq in [
                ('Factory A', 'USD', 'aluminium', False),
                ('Factory B', 'CNY', 'aluminium', True),
                ('Factory C', 'USD', 'steel', False),
                ('Factory D', 'USD', 'aluminium', True),
            ]:
                page.get_by_role('button', name='+ Add supplier quote').click()
                page.locator('#supplier').fill(name)
                values = {'unit_price':'18','currency':currency,'quoted_quantity':'100',
                          'moq':'' if missing_moq else '50','incoterm':'FOB','named_place':'Shenzhen',
                          'production_lead_days':'25','payment_terms':'30/70','valid_until':'2099-12-31'}
                source = '\n'.join(f'{k}: {v}' for k,v in values.items() if v) + '\nmaterial: ' + material
                page.locator('#source').fill(source)
                for k,v in values.items():
                    page.locator('#f_'+k).fill(v)
                    if v: page.locator('#e_'+k).fill(f'{k}: {v}')
                page.locator('#s_0').fill(material)
                page.locator('#se_0').fill('material: '+material)
                page.get_by_role('button', name='Save & validate quote').click()
                expect(page.locator('#message')).to_contain_text('Quote saved and validated')
            expect(page.locator('#comparison tr').first.locator('th')).to_have_count(5)
            expect(page.locator('.badge.comparable')).to_have_count(1)
            expect(page.locator('.badge.non_compliant')).to_have_count(1)
            expect(page.locator('.draft')).to_have_count(3)
            assert '最小起订量' in page.locator('.draft').first.input_value()
            page.reload()
            page.locator('.rfq-item').click()
            expect(page.locator('#comparison tr').first.locator('th')).to_have_count(5)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
