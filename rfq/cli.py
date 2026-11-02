"""Click commands for explicit human-operated RFQ outreach."""
import json
import click
from rfq.rfq_service import RFQService

def output(value):
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))

def service():
    return RFQService()

@click.group()
def rfq():
    """Draft, approve, send and compare requests for quotations."""

@rfq.command('create')
@click.option('--title', required=True)
@click.option('--product', required=True)
@click.option('--product-zh')
@click.option('--specification')
@click.option('--quantity')
@click.option('--unit')
@click.option('--target-incoterm')
@click.option('--destination')
@click.option('--require', 'certifications', multiple=True)
@click.option('--quote-deadline')
@click.option('--supplier-id', 'supplier_ids', multiple=True, type=int)
@click.option('--query')
@click.option('--limit', default=25, type=click.IntRange(1, 500))
@click.option('--channel', 'channels', multiple=True, type=click.Choice(['email', 'alibaba', 'made_in_china', 'whatsapp', 'wechat', 'contact_form']))
@click.option('--all-channels', is_flag=True)
def create(supplier_ids, query, limit, channels, all_channels, certifications, **values):
    s = service()
    campaign = s.create_campaign(**values, required_certifications=list(certifications))
    output({**campaign, 'drafting': s.draft_messages(campaign['id'], supplier_ids=list(supplier_ids) or None, query=query, limit=limit, channels=list(channels) or None, all_channels=all_channels)})

@rfq.command('list')
def list_campaigns():
    output(service().repo.rows('campaigns'))

@rfq.command('show')
@click.argument('campaign_id', type=int)
def show(campaign_id):
    output(service().show(campaign_id))

@rfq.command('approve')
@click.argument('campaign_id', type=int)
@click.option('--message-id', 'message_ids', multiple=True, type=int)
def approve(campaign_id, message_ids):
    output(service().approve(campaign_id, list(message_ids) or None))

@rfq.command('send')
@click.argument('campaign_id', type=int)
@click.option('--limit', type=click.IntRange(1, 500))
@click.option('--yes', is_flag=True)
def send(campaign_id, limit, yes):
    s = service()
    rows = s.repo.rows('messages', "campaign_id=? AND channel='email' AND status='approved'", (campaign_id,))
    if limit is not None:
        rows = rows[:limit]
    click.echo(f"{len(rows)} approved emails; remaining rolling 24h cap: {s.status()['remaining_cap']}")
    for row in rows:
        click.echo(row['recipient'])
    if yes or click.confirm('Send these approved emails?'):
        output(s.send_approved(campaign_id, limit))

@rfq.command('followups')
@click.argument('campaign_id', type=int)
@click.option('--after-days', default=3, type=click.IntRange(0, 365))
def followups(campaign_id, after_days):
    output(service().draft_followups(campaign_id, after_days))

@rfq.command('poll')
@click.option('--since-days', default=14, type=click.IntRange(1, 365))
@click.option('--limit', default=200, type=click.IntRange(1, 1000))
def poll(since_days, limit):
    output(service().poll_inbox(since_days, limit))

@rfq.command('reply')
@click.argument('message_id', type=int)
@click.option('--text', prompt=True)
def reply(message_id, text):
    output(service().record_manual_reply(message_id, text))

@rfq.command('mark-sent')
@click.argument('message_id', type=int)
def mark_sent(message_id):
    output(service().mark_sent(message_id))

@rfq.command('compare')
@click.argument('campaign_id', type=int)
def compare(campaign_id):
    output(service().compare(campaign_id))

@rfq.command('check')
def check():
    output(service().check())

@rfq.command('test-email')
@click.option('--to', required=True)
def test_email(to):
    output(service().test_email(to))
