"""HTTP controls and persisted jobs with fake adapters only."""
from fastapi.testclient import TestClient
from tests.test_always_on_outreach import loop, target, enrol
from tests.test_prospect_outreach import engine


def test_health_controls_preview_and_background_job(loop,monkeypatch):
    from api.app import app
    from api.always_on import get_loop
    from api.auth import require_api_token
    a,_,_=loop
    app.dependency_overrides[get_loop]=lambda:a
    app.dependency_overrides[require_api_token]=lambda:None
    client=TestClient(app)
    try:
        assert client.get('/health/outreach').status_code==200
        assert client.post('/outreach/always-on/pause',json={'paused':True}).json()['paused']
        assert client.post('/outreach/always-on/pause',json={'paused':False}).status_code==200
        assert client.post('/outreach/always-on/template/approve',json={'approved_by':'someone','note':'A corporate interest assessment'}).status_code==400
        enrol(loop)
        assert len(client.get('/outreach/always-on/preview').json()['emails'])==1
        a.tick=lambda:{'emails_sent':0}
        result=client.post('/outreach/always-on/jobs').json()
        job=a.repo.rows('SELECT * FROM pipeline_jobs WHERE id=?',(result['id'],))[0]
        assert job['status']=='completed'
    finally:
        app.dependency_overrides.clear()
