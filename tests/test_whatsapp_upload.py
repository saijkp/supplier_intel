from io import BytesIO
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from api.app import app
from api.whatsapp import get_whatsapp_inbox
from customers.whatsapp_inbox import WhatsAppInbox, MAX_IMAGE_BYTES
from storage.database import initialise_schema, connection_scope, MIGRATIONS

@pytest.fixture
def client(tmp_path, monkeypatch):
    import api.auth
    monkeypatch.setattr(api.auth, 'API_ACCESS_TOKEN', 'test-secret')
    path=tmp_path/'db.sqlite'
    initialise_schema(path)
    inbox=WhatsAppInbox(path)
    app.dependency_overrides[get_whatsapp_inbox]=lambda:inbox
    client=TestClient(app)
    yield client,inbox
    client.close()
    app.dependency_overrides.pop(get_whatsapp_inbox,None)

def png():
    out=BytesIO();Image.new('RGB',(20,20),'blue').save(out,'PNG');return out.getvalue()

AUTH={'Authorization':'Bearer test-secret'}

def test_auth_upload_and_private_retrieval(client):
    c,inbox=client
    assert c.post('/customers/whatsapp',files={'files':('x.png',png(),'image/png')}).status_code==401
    assert c.get('/customers/whatsapp').status_code==401
    r=c.post('/customers/whatsapp',headers=AUTH,files=[('files',('../../x.png',png(),'image/png'))],data={'text':'Please quote'})
    assert r.status_code==200,r.text
    card=r.json();assert card['mailbox']=='whatsapp' and card['text']=='Please quote'
    assert len(c.get('/customers/whatsapp',headers=AUTH).json())==1
    assert c.get('/customers/whatsapp/email-cards',headers=AUTH).status_code==200
    url='/customers/whatsapp/images/'+str(card['images'][0]['id'])
    assert c.get(url).status_code==401
    image=c.get(url,headers=AUTH)
    assert image.status_code==200 and image.headers['cache-control']=='no-store'
    assert Image.open(BytesIO(image.content)).size==(20,20)
    assert inbox.directory.parent==inbox.db_path.parent
    assert len(list(inbox.directory.iterdir()))==1

def test_type_size_and_link_limits(client):
    c,inbox=client
    for name,data,media,expected in [('x.svg',b'<svg/>','image/svg+xml',415),('x.png',b'not an image','image/png',415),('x.png',b'x'*(MAX_IMAGE_BYTES+1),'image/png',413),('x.heic',b'bad','image/heic',415),('x.png',png(),'image/jpeg',415)]:
        assert c.post('/customers/whatsapp',headers=AUTH,files={'files':(name,data,media)}).status_code==expected
    assert c.post('/customers/whatsapp',headers=AUTH,files={'files':('x.png',png(),'image/png')},data={'enquiry_id':99999}).status_code==404
    assert inbox.cards()==[]

def test_migration_upgrade_and_idempotence(tmp_path):
    path=tmp_path/'old.db';initialise_schema(path)
    with connection_scope(path) as conn:
        conn.execute('DROP TABLE whatsapp_images');conn.execute('DROP TABLE whatsapp_cards');conn.execute('DELETE FROM schema_migrations WHERE version=44')
    initialise_schema(path);initialise_schema(path)
    with connection_scope(path) as conn:
        assert conn.execute('SELECT count(*) FROM whatsapp_images').fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM schema_migrations WHERE version=44').fetchone()[0]==1
    assert 44 in MIGRATIONS


def test_downscale_and_multiple_image_limit(client):
    c,inbox=client
    data=BytesIO();Image.new('RGB',(3000,2400),'white').save(data,'PNG')
    r=c.post('/customers/whatsapp',headers=AUTH,files={'files':('large.png',data.getvalue(),'image/png')})
    assert r.status_code==200
    image_id=r.json()['images'][0]['id']
    image=c.get('/customers/whatsapp/images/'+str(image_id),headers=AUTH)
    assert Image.open(BytesIO(image.content)).size==(2048,1638)
    files=[('files',('x.png',png(),'image/png')) for _ in range(11)]
    assert c.post('/customers/whatsapp',headers=AUTH,files=files).status_code==400
    assert len(inbox.cards())==1

def test_email_cards_full_text_and_missing_received_time(client):
    from storage.customer_repository import CustomerRepository
    c,inbox=client
    repo=CustomerRepository(inbox.db_path)
    repo.insert('customer_inbox_leads',external_id='test:inbound',from_address='customer@example.test',subject='Plans',excerpt='Short preview',full_text='Full customer message',received_at=None,mailbox='sai@ikeng.co.uk')
    response=c.get('/customers/whatsapp/email-cards',headers=AUTH)
    assert response.headers['cache-control']=='no-store'
    card=response.json()[0]
    assert card['customer_name']=='customer@example.test'
    assert card['text']=='Full customer message'
    assert card['created_at']
