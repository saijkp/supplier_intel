"""Read-only daily queue, assembled solely from stored records."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from customers.whatsapp_inbox import WhatsAppInbox
from storage.rfq_repository import RFQRepository
from outreach.repository import OutreachRepository


def next_actions(service, at=None):
    at = at or datetime.now(timezone.utc)
    today = at.astimezone(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
    items, scheduled = [], []

    def add(key, who, label, time, action, view, due=None, **extra):
        item = dict(key=key, title=who, label=label, time=time, action=action,
                    view=view, due=due, **extra)
        item['urgency'] = 0 if due and due[:10] < today else 1 if due and due[:10] == today else 2
        items.append(item)

    for lead in service.list_leads():
        if not lead['already_replied']:
            add('lead-'+str(lead['id']), lead['from_name'] or lead['from_address'] or 'Website submission',
                lead['subject'] or 'New email', lead['received_at'] or lead['created_at'],
                'Create enquiry', 'inbox', lead=lead['id'], email=lead['from_address'] if lead['from_address'] and '@' in lead['from_address'] else None, can_create=bool(lead['from_name'] and lead['from_address']))
    latest_activity = {}
    for activity in service.repo.rows('customer_activities', order='occurred_at, id'):
        latest_activity[activity['enquiry_id']] = activity
    for e in service.list_enquiries():
        due = e.get('next_action_due')
        if (e.get('next_action_snoozed_until') or '') > today or (e['status'] == 'not responding' and due and due[:10] > today):
            scheduled.append({'title': e['customer_name'], 'action': e['next_action'], 'due': e['next_action_due']})
            continue
        if e['waiting_on'] == 'us' or (due and due[:10] <= today):
            add('enquiry-'+str(e['id']), e['customer_name'], e['next_action'] or e['title'],
                e['updated_at'], 'Open enquiry', 'customers', due, enquiry=e['id'],
                happened={'email_in':'Customer emailed', 'whatsapp_in':'Customer messaged', 'call_in':'Customer called', 'email_out':'Email sent', 'whatsapp_out':'WhatsApp sent', 'call_out':'Call logged'}.get(latest_activity.get(e['id'], {}).get('kind'), 'Enquiry updated'),
                email=e['customer_email'], phone=e['customer_phone'])
        elif due:
            scheduled.append({'title': e['customer_name'], 'action': e['next_action'], 'due': e['next_action_due']})
    for card in WhatsAppInbox(service.repo.db_path).cards():
        if not card['enquiry_id']:
            add('whatsapp-'+str(card['id']), 'WhatsApp screenshot', 'Review message and identify customer',
                card['created_at'], 'Review screenshot', 'inbox', whatsapp=card['id'])
    rfq = RFQRepository(service.repo.db_path)
    campaigns = {c['id']: c for c in rfq.rows('campaigns') if c['id'] != 1}
    replies = rfq.rows('replies')
    for r in replies:
        if r['supplier_id'] != 3505 and r['campaign_id'] in campaigns and r['parse_status'] in ('pending', 'failed') and r['reply_type'] not in ('bounce', 'opt_out', 'auto_reply'):
            add('reply-'+str(r['id']), r['from_address'] or campaigns[r['campaign_id']]['title'],
                'Supplier reply needs review', r['received_at'], 'Read reply', 'quotes',
                campaign=r['campaign_id'], reply=r['id'])
    latest = {}
    for m in rfq.rows('messages'):
        if m['status'] == 'sent':
            latest[(m['campaign_id'], m['supplier_id'])] = m
    for m in latest.values():
        if m['supplier_id'] == 3505 or m['campaign_id'] not in campaigns or m['status'] != 'sent' or not m['sent_at']:
            continue
        sent_at = datetime.fromisoformat(m['sent_at'].replace('Z', '+00:00'))
        sent_at = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at.astimezone(timezone.utc)
        chase_at = (sent_at + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        has_reply = any(r['campaign_id']==m['campaign_id'] and r['supplier_id']==m['supplier_id'] and r['reply_type'] not in ('bounce','auto_reply') for r in replies)
        if not has_reply and chase_at > at.strftime('%Y-%m-%d %H:%M:%S'):
            scheduled.append({'title': m['recipient'] or campaigns[m['campaign_id']]['title'], 'action':'Check supplier reply', 'due':chase_at})
        if chase_at <= at.strftime('%Y-%m-%d %H:%M:%S') and not has_reply:
            add('chase-'+str(m['id']), m['recipient'] or campaigns[m['campaign_id']]['title'],
                'No supplier reply after 24 hours', m['sent_at'], 'Review chase', 'quotes', chase_at, campaign=m['campaign_id'])
    tasks = OutreachRepository(service.repo.db_path).rows("SELECT e.*,a.company FROM outreach_events e LEFT JOIN prospect_accounts a ON a.id=e.account_id WHERE e.status='pending' AND e.kind IN ('linkedin_task','visit_task','call_task','needs Sai')")
    for task in tasks:
        if not task['due_at'] or task['due_at'][:10] <= today:
            add('outreach-'+str(task['id']), task['company'] or 'Reply needs matching', task['kind'].replace('_',' '),
                task['created_at'], 'Open task', 'outreach', task['due_at'], task=task['id'])
        else:
            scheduled.append({'title': task['company'] or 'Outreach', 'action': task['kind'].replace('_', ' '), 'due': task['due_at']})
    items.sort(key=lambda i: (i['urgency'], i['due'] or i['time'] or '', i['key']))
    scheduled.sort(key=lambda e: e['due'])
    from config import settings as cfg
    from rfq.zoho_mail import missing, REGIONS
    configured = (cfg.CUSTOMER_ZOHO_ADDRESS.lower() in service._mailboxes) if service._mailboxes is not None else (
        not missing('CUSTOMER_ZOHO_') and cfg.CUSTOMER_ZOHO_REGION in REGIONS and cfg.CUSTOMER_ZOHO_ACCOUNT_ID.isdigit())
    return {'can_email': bool(cfg.RFQ_SEND_ENABLED and configured), 'items': items, 'next_scheduled': (scheduled[0] if scheduled else None)}
