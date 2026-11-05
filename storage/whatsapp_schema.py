WHATSAPP_SCHEMA = '''CREATE TABLE IF NOT EXISTS whatsapp_inbound (
 id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL UNIQUE,
 sender TEXT, kind TEXT, body TEXT, sent_at TEXT,
 supplier_id INTEGER REFERENCES suppliers(id), rfq_id TEXT REFERENCES rfqs(id),
 reviewed_at TEXT, received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)'''
WHATSAPP_INDEX = 'CREATE INDEX IF NOT EXISTS whatsapp_inbound_received ON whatsapp_inbound(received_at, id)'
