"""Opt-in, durable weekday review-only research job; never enrols or sends."""
import csv
import json
import os
from datetime import timedelta
from customers.mailboxes import UK

DAILY_SQL = """CREATE TABLE IF NOT EXISTS planning_firm_daily (
 day TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
 status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}');"""
METRICS = ('firms', 'verified', 'named_people', 'direct_emails', 'serp_calls', 'reveals')
PRIORITY = ['Westminster', 'Lambeth', 'Barnet', 'Brent', 'Tower Hamlets', 'Hackney', 'Trafford', 'Bolton', 'Stockport']
def all_areas():
    from planning.service import LONDON, MANCHESTER
    return PRIORITY + [a for a in LONDON + MANCHESTER if a not in PRIORITY]


def authorities(day):
    # Every authority once per week, priority councils distributed across weekdays.
    return all_areas()[day.weekday()::5]


def snapshot(repo):
    from planning.firm_trial import firm_report
    report = firm_report(repo)
    return dict(firms=report['firms'], verified=sum(bool(f['payload'].get('CH_verified') and f['payload'].get('website_verified')) for f in report['firm_records']),
                named_people=sum(len({p['name'].casefold() for p in f['payload'].get('people', []) if p.get('name')}) for f in report['firm_records']),
                direct_emails=report['direct_email'], serp_calls=report['serp_calls'], reveals=report['reveals'])


def tick(planning, trial_factory=None):
    if os.getenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED', '').lower() != 'true':
        return {'status': 'disabled'}
    now = planning.outreach.now().astimezone(UK)
    if now.weekday() >= 5 or (now.hour, now.minute) < (8, 30):
        return {'status': 'outside_window'}
    day = now.date().isoformat()
    previous = planning.repo.rows('SELECT payload FROM planning_firm_daily WHERE day<? ORDER BY day DESC LIMIT 1', (day,))
    prior = json.loads(previous[0]['payload']) if previous else {}
    from planning.planit_client import PlanItClient
    PlanItClient._retry_at = max(PlanItClient._retry_at, prior.get('planit_retry_at', 0))
    if PlanItClient._retry_at > now.timestamp():
        return {'status': 'planit_cooldown', 'retry_at': PlanItClient._retry_at}
    selected = authorities(now.date())
    errors = prior.get('sync', {}).get('errors', {})
    old_areas = prior.get('authorities', [])
    if errors and old_areas:
        first = min((old_areas.index(a) for a in errors if a in old_areas), default=len(old_areas))
        selected = list(dict.fromkeys(old_areas[first:] + selected))
    from storage.database import connection_scope
    with connection_scope(planning.repo.db_path) as db:
        claimed = db.execute("INSERT OR IGNORE INTO planning_firm_daily(day,started_at,status) VALUES(?,?,'running')", (day, now.isoformat())).rowcount
    if not claimed:
        return {'status': 'already_attempted'}
    before = snapshot(planning.repo)
    payload = {'authorities': selected, 'counters': dict.fromkeys(METRICS, 0)}
    status = 'failed'
    try:
        payload['sync'] = planning.sync_recent(areas=payload['authorities'], trial_only=True)
        from planning.firm_trial import FirmTrial
        trial = (trial_factory or FirmTrial)(planning)
        trial.run(limit=100, max_searches=40, max_reveals=20)
        status = 'completed' if payload['sync'].get('complete') else 'partial'
    except Exception as exc:
        payload['error'] = type(exc).__name__
    finally:
        payload['planit_retry_at'] = PlanItClient._retry_at
        after = snapshot(planning.repo)
        payload['counters'] = {k: max(0, after[k] - before[k]) for k in METRICS}
        planning.repo.execute('UPDATE planning_firm_daily SET completed_at=?,status=?,payload=? WHERE day=?',
                              (planning.outreach.now().isoformat(), status, json.dumps(payload), day))
    return {'status': status, **payload}


def health(repo, now):
    day = now.astimezone(UK).date()
    days = []
    cursor = day
    while len(days) < 5:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    rows = repo.rows('SELECT * FROM planning_firm_daily WHERE day>=? AND day<=? ORDER BY day', (min(days), day.isoformat()))
    today = next((r for r in rows if r['day'] == day.isoformat()), None)
    today = {**today, **json.loads(today['payload'])} if today else {'day': day.isoformat(), 'status': 'not_run', 'counters': dict.fromkeys(METRICS, 0)}
    totals = {k: sum(json.loads(r['payload']).get('counters', {}).get(k, 0) for r in rows) for k in METRICS}
    return {'enabled': os.getenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED', '').lower() == 'true',
            'today': today, 'five_day_totals': totals, 'people_target': 100, 'target_window': 'five_weekdays',
            'people_remaining': max(0, 100 - totals['named_people']), 'days': days}


def export_contacts(repo, output):
    from planning.firm_trial import firm_report
    fields = ['firm', 'person', 'role', 'email', 'website', 'CH number', 'project refs + addresses', 'evidence URLs', 'review status']
    with open(output, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for firm in firm_report(repo)['firm_records']:
            data = firm['payload']
            projects = repo.rows('SELECT p.* FROM planning_projects p JOIN planning_firm_projects l ON l.project_id=p.id WHERE l.firm_key=?', (firm['firm_key'],))
            for person in data.get('people', []) + data.get('field_provenance', {}).get('people_candidates', []):
                writer.writerow(dict(zip(fields, [firm['company'], person.get('name'), person.get('role'), person.get('email') or person.get('email_candidate'), data.get('website') or data.get('field_provenance', {}).get('website_candidate'), firm['company_number'],
                    '; '.join(str(p['reference']) + ' | ' + str(p['address'] or '') for p in projects),
                    '; '.join(filter(None, [data.get('ch_source'), person.get('source_url')] + [p.get('source_url') for p in data.get('website_evidence', {}).get('pages', [])] + [p['source_url'] for p in projects])), data.get('review_status', 'trial_review')])))
