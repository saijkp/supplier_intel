"""Connect existing outreach to persistent comparison and bounded follow-ups."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import secrets
import sqlite3

from rfq.rfq_service import RFQService
from rfq.quote_parser import strip_history
from rfq.workspace_parser import WorkspaceParser
from rfq.templates import header
from sourcing.workspace_review import reviewed
from storage.quote_workspace_repository import QuoteWorkspaceRepository
from storage.rfq_repository import RFQRepository
from storage.rfq_workflow_repository import WorkflowRepository
from storage.repository import SupplierRepository


def timestamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def latest_quotes(rfq):
    return {q['supplier_id']:q for q in rfq['quotes'] if q.get('supplier_id')}


def clarification_send_reason(db_path, message):
    """Mandatory send-time guard, including sends requested through legacy routes."""
    repo=WorkflowRepository(db_path); record=repo.clarification(message['id'])
    if not record: return 'Clarification has no auditable workflow record'
    policy=repo.get(record['rfq_id'])
    outreach=RFQRepository(db_path)
    if outreach.get('campaigns',message['campaign_id'])['status']!='open':return 'Campaign is closed'
    if outreach.suppressed(message['recipient']):return 'Address is suppressed'
    supplier=SupplierRepository(db_path).get_supplier(record['supplier_id'])
    if not supplier or supplier.get('flagged'):return 'Supplier is flagged or missing'
    if record['automatic'] and (not policy['poll_enabled'] or not policy['auto_send'] or not policy['expires_at'] or policy['expires_at']<=timestamp()):
        return 'Automatic follow-ups paused or expired'
    if record['automatic']:
        if record['supplier_id'] not in json.loads(policy['approved_supplier_ids']):return 'Supplier is outside the approved follow-up scope'
        used=sum(1 for m in repo.clarifications(record['rfq_id']) if m['supplier_id']==record['supplier_id'] and m['id']!=message['id'] and m['status'] not in ('draft','approved','skipped'))
        if used>=policy['max_rounds']:return 'Automatic follow-up limit reached'
    rfq=reviewed(QuoteWorkspaceRepository(db_path).get_rfq(record['rfq_id']))
    quote=latest_quotes(rfq).get(record['supplier_id'])
    if not quote or quote['id']!=record['quote_id']: return 'A newer quote superseded this clarification'
    if quote['review']['status']=='comparable': return 'Supplier quote is already complete'
    if quote.get('manual_review'): return 'Quote requires human review'
    if any(r['supplier_id']==record['supplier_id'] for r in repo.unprocessed(record['rfq_id'])):
        return 'A newer supplier reply is awaiting validation'
    return None


class WorkspaceWorkflow:
    def __init__(self, db_path=None, outreach=None, parser=None):
        self.repo=WorkflowRepository(db_path)
        self.workspace=QuoteWorkspaceRepository(db_path)
        self.outreach=outreach if outreach is not None else RFQService(repo=RFQRepository(db_path),defer_parsing=True)
        self.parser=parser if parser is not None else WorkspaceParser()

    def load(self,rid):
        rfq=self.workspace.get_rfq(rid)
        if not rfq: raise ValueError('RFQ not found')
        return reviewed(rfq)

    def link(self,rid,campaign_id,confirmed):
        rfq=self.load(rid); campaign=self.outreach.repo.get('campaigns',campaign_id)
        if not confirmed: raise ValueError('Confirm that this campaign uses the master RFQ specification')
        if campaign['status']!='open': raise ValueError('Campaign is closed')
        try:
            if Decimal(str(campaign.get('quantity')))!=Decimal(rfq['quantity']): raise ValueError('Campaign quantity differs from the master RFQ')
        except InvalidOperation:
            raise ValueError('Campaign needs an explicit numeric quantity') from None
        if (campaign.get('unit') or '').strip().casefold()!=rfq['unit'].strip().casefold():
            raise ValueError('Campaign unit differs from the master RFQ')
        if self.repo.get(rid): raise ValueError('This RFQ already has an outreach campaign')
        try: self.repo.link(rid,campaign_id)
        except sqlite3.IntegrityError: raise ValueError('Campaign is already linked to another RFQ') from None
        return self.view(rid)

    def create(self,rid,supplier_ids,query=None,channels=None):
        rfq=self.load(rid)
        if self.repo.get(rid): raise ValueError('This RFQ already has an outreach campaign')
        if not supplier_ids and not (query or '').strip(): raise ValueError('Select suppliers or enter a supplier search term')
        campaign=self.outreach.create_campaign(title=rfq['title'],product=rfq['title'],quantity=rfq['quantity'],unit=rfq['unit'],
                     specification='\n'.join(k+': '+v for k,v in rfq['mandatory_specs'].items()))
        self.repo.link(rid,campaign['id'])
        self.outreach.draft_messages(campaign['id'],supplier_ids=supplier_ids or None,query=query,limit=25,channels=channels)
        return self.view(rid)

    def add_contact(self,rid,name,email,whatsapp,channel):
        self.load(rid)
        if not name.strip():raise ValueError('Enter the supplier name')
        email=email.strip().lower();whatsapp=re.sub(r'\D','',whatsapp)
        if email and not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+',email):raise ValueError('Enter a single valid supplier email address')
        if whatsapp and not 8<=len(whatsapp)<=15:raise ValueError('Use the supplier WhatsApp number with its country code')
        if channel=='email' and not email:raise ValueError('Email contact requires an email address')
        if channel=='whatsapp' and not whatsapp:raise ValueError('WhatsApp contact requires a phone number')
        existing=self.repo.find_contact(email,whatsapp)
        if existing and existing['flagged']:raise ValueError('This contact belongs to a flagged supplier; review the existing record')
        if existing:
            supplier=self.outreach.suppliers.get_supplier(existing['id'])
            if not supplier.get('primary_email' if channel=='email' else 'whatsapp'):
                raise ValueError('The existing supplier has no confirmed contact for this channel. Review their contact record first.')
        sid=existing['id'] if existing else self.outreach.suppliers.create_golden_record({'canonical_name':name,'primary_email':email or None,'whatsapp':whatsapp or None,'discovery_source':'manual_rfq_contact'})
        link=self.repo.get(rid)
        if link:
            self.repo.policy(rid,poll_enabled=0,auto_send=0)
            self.outreach.draft_messages(link['campaign_id'],supplier_ids=[sid],channels=[channel])
        else:self.create(rid,[sid],channels=[channel])
        return self.view(rid)

    def view(self,rid):
        rfq=self.load(rid); link=self.repo.get(rid)
        readiness=self.outreach.status()
        readiness['extraction_mode']='AI with evidence checks' if self.parser.use_model else 'Labelled text only; other replies need review'
        if not link: return {'linked':False,'readiness':readiness}
        safe={k:v for k,v in link.items() if k not in {'lease_token','lease_until'}}
        campaign=self.outreach.show(link['campaign_id'])
        latest=latest_quotes(rfq)
        participants={m['supplier_id'] for m in campaign['messages'] if m['kind']=='initial' and m['supplier_id']}
        campaign['progress']={'suppliers':len(participants),'complete':sum(1 for sid in participants if sid in latest and latest[sid]['review']['status']=='comparable')}
        return {'linked':True,'policy':safe,'readiness':readiness,'campaign':campaign,
                'ingestions':self.repo.ingestions(rid),'clarifications':self.repo.clarifications(rid)}

    def set_policy(self,rid,*,poll_enabled,auto_send,interval_minutes,followup_hours,max_rounds,days,approve_scope):
        if not self.repo.get(rid): raise ValueError('Link an outreach campaign first')
        ready=self.outreach.status()
        if auto_send and (not poll_enabled or not approve_scope):
            raise ValueError('Approve the selected campaign, cadence and maximum follow-up count before enabling automatic sending')
        if poll_enabled and not ready['imap_configured']: raise ValueError('Configure the dedicated RFQ inbox on the server first')
        if auto_send and not ready['ready']: raise ValueError('Sending is disabled or mailbox / buyer identity configuration is incomplete')
        expiry=(datetime.now(timezone.utc)+timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        campaign=self.outreach.show(self.repo.get(rid)['campaign_id'])
        approved=sorted({m['supplier_id'] for m in campaign['messages'] if m['kind']=='initial' and m['channel']=='email' and m['supplier_id']})
        if auto_send and not approved:raise ValueError('This campaign has no email suppliers. WhatsApp remains assisted.')
        self.repo.policy(rid,poll_enabled=int(poll_enabled),auto_send=int(auto_send),interval_minutes=interval_minutes,
                         followup_hours=followup_hours,max_rounds=max_rounds,expires_at=expiry,next_poll_at=timestamp(),approved_supplier_ids=json.dumps(approved if auto_send else []))
        if auto_send:
            latest=latest_quotes(self.load(rid))
            for draft in self.repo.clarifications(rid):
                quote=latest.get(draft['supplier_id'])
                # Only unchanged deterministic drafts fall within this policy approval.
                if draft['status']=='draft' and quote and quote['id']==draft['quote_id'] and not quote.get('manual_review') and quote['review']['status']!='comparable':
                    expected=quote['clarification_zh']+'\nReference: '+draft['reference']
                    if draft['body']==expected and draft['subject']==header('['+draft['reference']+'] 报价信息确认: '+self.load(rid)['title']):
                        self.repo.approve_automatic(draft['id'])
        return self.view(rid)

    def process_replies(self,rid):
        count=0
        for reply in self.repo.unprocessed(rid)[:10]:
            if reply['reply_type'] in ('bounce','opt_out'):
                self.repo.ingest(rid,reply,'ignored',detail=reply['reply_type']); continue
            rfq=self.load(rid); previous=latest_quotes(rfq).get(reply['supplier_id'])
            text=reply['body_text'] or ''
            source=text if reply['source']=='imap' else strip_history(text)
            extracted=self.parser.parse(source,rfq['mandatory_specs'])
            attachments=json.loads(reply['attachments'] or '[]')
            manual=extracted['state']!='parsed' or any(a.get('read_status')=='needs_manual_review' for a in attachments)
            declined=bool(re.search(r'\b(cannot quote|cannot supply|unable to quote|unable to supply)\b|无法报价|不接受此订单',source,re.I))
            offers=extracted['offers']
            candidate=offers[0] if len(offers)==1 else {'fields':{},'quoted_specs':{},'evidence':{}}
            fields=dict((previous or {}).get('fields',{})); specs=dict((previous or {}).get('quoted_specs',{}))
            evidence=dict((previous or {}).get('evidence',{})); conflicts={}
            for group,target in [('fields',fields),('quoted_specs',specs)]:
                for key,value in candidate[group].items():
                    proof_key='spec:'+key if group=='quoted_specs' else key
                    equal=str(target.get(key)).strip().casefold()==str(value).strip().casefold()
                    if group=='fields' and key in {'unit_price','moq','quoted_quantity','production_lead_days'} and target.get(key) is not None:
                        try: equal=Decimal(str(target[key]))==Decimal(str(value))
                        except InvalidOperation: pass
                    if key in target and target[key] is not None and not equal:
                        # A new snapshot records disagreements; existing confirmations remain intact.
                        conflicts[proof_key]={'previous':target[key],'candidate':value,'evidence':candidate['evidence'][proof_key]}
                    else:
                        target[key]=value; evidence[proof_key]=candidate['evidence'][proof_key]
            manual=manual or bool(conflicts) or declined
            if previous and previous.get('manual_review'): manual=True
            supplier=self.outreach.suppliers.get_supplier(reply['supplier_id']) or {}
            prior_source=(previous or {}).get('source_text','')
            combined=(prior_source+'\n\n--- Supplier reply '+str(reply['id'])+' ---\n'+source).strip()
            if len(combined)>200000:
                manual=True
            payload={'supplier_id':reply['supplier_id'],'supplier_name':supplier.get('canonical_name','Supplier '+str(reply['supplier_id'])),
                     'source_text':combined,'fields':fields,'quoted_specs':specs,'evidence':evidence,
                     'source_reply_ids':list(dict.fromkeys([*(previous or {}).get('source_reply_ids',[]),reply['id']])),
                     'conflicts':conflicts,'manual_review':manual,'deviations':['Human review required: '+('Supplier declined' if declined else 'Conflicting confirmations or extraction / attachment issue')] if manual else []}
            legacy=candidate['fields'] if candidate['fields'].get('unit_price') else None
            inserted=self.repo.ingest(rid,reply,'needs_review' if manual else 'parsed',payload,legacy,extracted.get('detail',''))
            if inserted and legacy:
                self.outreach._outcome(self.outreach.repo.get('messages',reply['message_id']),'quoted')
            count+=1
        return count

    def make_drafts(self,rid):
        rfq=self.load(rid); link=self.repo.get(rid); campaign=self.outreach.show(link['campaign_id'])
        latest=latest_quotes(rfq); self.repo.cancel_stale(rid,latest)
        pending={r['supplier_id'] for r in self.repo.unprocessed(rid)}
        created=[]; since=timestamp()
        for message in campaign['messages']:
            if message['kind']!='initial' or message['channel']!='email' or not message['sent_at']: continue
            sid=message['supplier_id']; quote=latest.get(sid)
            if sid in pending: continue
            if not quote and not any(r['supplier_id']==sid for r in campaign['replies']):
                self.workspace.add_quote(rid,{'supplier_id':sid,'supplier_name':message['supplier_name'],
                    'fields':{},'quoted_specs':{},'evidence':{},'source_text':'','deviations':[], 'awaiting_reply':True})
                quote=latest_quotes(self.load(rid)).get(sid)
            if not quote or quote['review']['status']=='comparable' or quote.get('manual_review'): continue
            if self.outreach.repo.suppressed(message['recipient']): continue
            supplier=self.outreach.suppliers.get_supplier(sid)
            if not supplier or supplier.get('flagged'): continue
            history=[m for m in campaign['messages'] if m['supplier_id']==sid and m['sent_at']]
            last=max(m['sent_at'] for m in history)
            due=(datetime.strptime(last,'%Y-%m-%d %H:%M:%S')+timedelta(hours=link['followup_hours'])).strftime('%Y-%m-%d %H:%M:%S')
            if due>since: continue
            reference=f'RFQ-{link["campaign_id"]}-'+secrets.token_hex(3).upper()
            # Only selected missing fields/deviations enter this message, never supplier instructions.
            mid=self.repo.draft(rid,sid,quote['id'],{'recipient':message['recipient'],
                'subject':header('['+reference+'] 报价信息确认: '+rfq['title']),
                'body':quote['clarification_zh']+'\nReference: '+reference,'reference':reference},bool(link['auto_send']) and sid in json.loads(link['approved_supplier_ids']))
            if mid: created.append(mid)
        return created

    def sync(self,rid,poll=True):
        link=self.repo.get(rid)
        if not link: raise ValueError('Link an outreach campaign first')
        lease=self.repo.claim(rid)
        if not lease: return {'busy':True}
        error=None
        try:
            campaign=self.outreach.repo.get('campaigns',link['campaign_id'])
            if campaign['status']!='open':
                self.repo.policy(rid,poll_enabled=0,auto_send=0)
                return {'stopped':'Campaign closed'}
            if link['expires_at'] and link['expires_at']<=timestamp():
                self.repo.policy(rid,poll_enabled=0,auto_send=0)
                return {'stopped':'Follow-up authorization expired'}
            inbox={'skipped':'Inbox is not configured'}
            if poll and self.outreach.status()['imap_configured']:
                inbox=self.outreach.poll_inbox(since_days=14,limit=50,campaign_id=link['campaign_id'],strict_sender=True)
            ingested=self.process_replies(rid)
            mids=self.make_drafts(rid)
            auto_ids=[m['id'] for m in self.repo.clarifications(rid) if m['automatic'] and m['status']=='approved']
            sent=self.outreach.send_approved(link['campaign_id'],message_ids=auto_ids) if auto_ids else {'sent':0}
            view=self.view(rid)
            progress=view['campaign']['progress']
            if progress['suppliers'] and progress['complete']==progress['suppliers']:
                self.repo.policy(rid,poll_enabled=0,auto_send=0)
            return {'inbox':inbox,'ingested':ingested,'drafted':len(mids),'sending':sent,'progress':progress}
        except Exception:
            error='Inbox or workflow failed; check server configuration. Original replies remain available.'
            # Never return transport exception text, which can contain mailbox credentials.
            return {'error':error}
        finally:
            self.repo.finish(rid,lease,error)
