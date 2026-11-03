"""Atomic persistence for the inbox-to-comparison workflow."""
import json
import uuid

from storage.database import connection_scope


class WorkflowRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def link(self, rid, campaign_id):
        with connection_scope(self.db_path) as c:
            c.execute('INSERT INTO rfq_workspace_outreach(rfq_id,campaign_id) VALUES (?,?)', (rid, campaign_id))
        return self.get(rid)

    def get(self, rid):
        with connection_scope(self.db_path) as c:
            row = c.execute('SELECT * FROM rfq_workspace_outreach WHERE rfq_id=?', (rid,)).fetchone()
            return dict(row) if row else None

    def policy(self, rid, **values):
        allowed = {'poll_enabled','auto_send','approved_supplier_ids','interval_minutes','followup_hours','max_rounds','expires_at','next_poll_at'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid workflow policy')
        with connection_scope(self.db_path) as c:
            c.execute('UPDATE rfq_workspace_outreach SET '+','.join(k+'=?' for k in values)+' WHERE rfq_id=?', (*values.values(), rid))
        return self.get(rid)

    def due(self):
        with connection_scope(self.db_path) as c:
            return [dict(r) for r in c.execute("SELECT * FROM rfq_workspace_outreach WHERE poll_enabled=1 AND (next_poll_at IS NULL OR next_poll_at<=CURRENT_TIMESTAMP)")]

    def find_contact(self,email,whatsapp):
        with connection_scope(self.db_path) as c:
            rows=c.execute('SELECT id,primary_email,whatsapp,flagged FROM suppliers').fetchall()
        import re
        for row in rows:
            if (email and (row['primary_email'] or '').strip().lower()==email.lower()) or (whatsapp and re.sub(r'\D','',row['whatsapp'] or '')==whatsapp):
                return dict(row)
        return None

    def claim(self, rid):
        token = str(uuid.uuid4())
        with connection_scope(self.db_path) as c:
            changed = c.execute("UPDATE rfq_workspace_outreach SET lease_token=?,lease_until=datetime('now','+2 hours') WHERE rfq_id=? AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)", (token,rid)).rowcount
        return token if changed else None

    def finish(self, rid, token, error=None):
        with connection_scope(self.db_path) as c:
            c.execute("UPDATE rfq_workspace_outreach SET last_run_at=CURRENT_TIMESTAMP,last_error=?,next_poll_at=datetime('now','+'||interval_minutes||' minutes'),lease_token=NULL,lease_until=NULL WHERE rfq_id=? AND lease_token=?", (error,rid,token))

    def unprocessed(self, rid):
        with connection_scope(self.db_path) as c:
            return [dict(r) for r in c.execute('SELECT r.* FROM rfq_replies r JOIN rfq_workspace_outreach w ON w.campaign_id=r.campaign_id LEFT JOIN rfq_workspace_ingestions i ON i.rfq_id=w.rfq_id AND i.reply_id=r.id WHERE w.rfq_id=? AND i.reply_id IS NULL ORDER BY r.id', (rid,))]

    def ingestions(self, rid):
        with connection_scope(self.db_path) as c:
            return [dict(r) for r in c.execute('SELECT * FROM rfq_workspace_ingestions WHERE rfq_id=? ORDER BY reply_id', (rid,))]

    def ingest(self, rid, reply, state, payload=None, legacy=None, detail=''):
        with connection_scope(self.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM rfq_workspace_ingestions WHERE rfq_id=? AND reply_id=?', (rid,reply['id'])).fetchone():
                return False
            if payload is not None:
                c.execute('INSERT INTO rfq_workspace_quotes(id,rfq_id,payload) VALUES (?,?,?)', (str(uuid.uuid4()),rid,json.dumps(payload,ensure_ascii=False)))
            if legacy and not c.execute('SELECT 1 FROM rfq_quotes WHERE reply_id=?', (reply['id'],)).fetchone():
                c.execute('INSERT INTO rfq_quotes(reply_id,campaign_id,supplier_id,unit_price,currency,moq,payment_terms,valid_until,incoterm,evidence) VALUES (?,?,?,?,?,?,?,?,?,?)',
                          (reply['id'],reply['campaign_id'],reply['supplier_id'],legacy.get('unit_price'),legacy.get('currency'),legacy.get('moq'),legacy.get('payment_terms'),legacy.get('valid_until'),legacy.get('incoterm'),json.dumps(payload['evidence'])))
            c.execute('INSERT INTO rfq_workspace_ingestions(rfq_id,reply_id,state,detail) VALUES (?,?,?,?)', (rid,reply['id'],state,detail))
            c.execute('UPDATE rfq_replies SET parse_status=? WHERE id=?', ('needs_manual_review' if state=='needs_review' else 'parsed' if payload else 'no_quote',reply['id']))
            if legacy:
                c.execute("UPDATE rfq_replies SET reply_type='quote' WHERE id=?",(reply['id'],))
        return True

    def clarifications(self, rid):
        with connection_scope(self.db_path) as c:
            return [dict(r) for r in c.execute('SELECT m.*, x.quote_id,x.round,x.automatic FROM rfq_messages m JOIN rfq_workspace_clarifications x ON x.message_id=m.id WHERE x.rfq_id=? ORDER BY m.id', (rid,))]

    def draft(self, rid, supplier_id, quote_id, content, automatic=False):
        with connection_scope(self.db_path) as c:
            c.execute('BEGIN IMMEDIATE')
            policy = c.execute('SELECT * FROM rfq_workspace_outreach WHERE rfq_id=?', (rid,)).fetchone()
            pending = c.execute("SELECT 1 FROM rfq_workspace_clarifications x JOIN rfq_messages m ON m.id=x.message_id WHERE x.rfq_id=? AND x.supplier_id=? AND m.status IN ('draft','approved','sending','failed')", (rid,supplier_id)).fetchone()
            used=c.execute("SELECT count(*) FROM rfq_workspace_clarifications x JOIN rfq_messages m ON m.id=x.message_id WHERE x.rfq_id=? AND x.supplier_id=? AND m.status NOT IN ('skipped','draft','approved')",(rid,supplier_id)).fetchone()[0]
            rounds=c.execute('SELECT coalesce(max(round),0) FROM rfq_workspace_clarifications WHERE rfq_id=? AND supplier_id=?',(rid,supplier_id)).fetchone()[0]
            if pending or used >= policy['max_rounds']:
                return None
            status = 'approved' if automatic and policy['auto_send'] and policy['poll_enabled'] else 'draft'
            mid = c.execute("INSERT INTO rfq_messages(campaign_id,supplier_id,channel,kind,language,recipient,subject,body,reference,status,approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,CASE WHEN ?='approved' THEN CURRENT_TIMESTAMP END)",
                            (policy['campaign_id'],supplier_id,'email','clarification:'+str(uuid.uuid4()),'zh',content['recipient'],content['subject'],content['body'],content['reference'],status,status)).lastrowid
            c.execute('INSERT INTO rfq_workspace_clarifications(message_id,rfq_id,supplier_id,quote_id,round,automatic) VALUES (?,?,?,?,?,?)', (mid,rid,supplier_id,quote_id,rounds+1,int(status=='approved')))
            return mid

    def clarification(self, mid):
        with connection_scope(self.db_path) as c:
            row = c.execute('SELECT * FROM rfq_workspace_clarifications WHERE message_id=?', (mid,)).fetchone()
            return dict(row) if row else None

    def approve_manual(self,mid):
        with connection_scope(self.db_path) as c:
            c.execute('UPDATE rfq_workspace_clarifications SET automatic=0 WHERE message_id=?',(mid,))

    def approve_automatic(self,mid):
        with connection_scope(self.db_path) as c:
            changed=c.execute("UPDATE rfq_messages SET status='approved',approved_at=CURRENT_TIMESTAMP WHERE id=? AND status='draft'",(mid,)).rowcount
            if changed:c.execute('UPDATE rfq_workspace_clarifications SET automatic=1 WHERE message_id=?',(mid,))

    def cancel_stale(self, rid, latest):
        with connection_scope(self.db_path) as c:
            for item in self.clarifications(rid):
                q = latest.get(item['supplier_id'])
                if item['status'] in ('draft','approved') and (not q or q['id'] != item['quote_id'] or q['review']['status']=='comparable' or q.get('manual_review')):
                    c.execute("UPDATE rfq_messages SET status='skipped',status_reason='Superseded or requires review' WHERE id=? AND status IN ('draft','approved')", (item['id'],))
