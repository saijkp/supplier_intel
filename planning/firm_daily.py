"""Opt-in, durable weekday review-only research job; never enrols or sends."""
import csv
import json
import os
from datetime import datetime, timedelta
from customers.mailboxes import UK

DAILY_SQL = """CREATE TABLE IF NOT EXISTS planning_firm_daily (
 day TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
 status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}');"""
METRICS = ('firms', 'architect_firms', 'CH_verified', 'website_verified', 'verified_people', 'named_people_any', 'ch_directors', 'direct_emails', 'generic_emails', 'needs_review', 'serp_calls', 'reveals', 'est_cost_gbp', 'apollo_credits')
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
    return {k: report[k] for k in METRICS} | {'verified_people_keys': report['verified_people_keys']}


def tick(planning, trial_factory=None):
    if os.getenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED', '').lower() != 'true':
        return {'status': 'disabled'}
    now = planning.outreach.now().astimezone(UK)
    # Persist the first enabled observation across restarts, without claiming a run.
    from storage.database import connection_scope
    with connection_scope(planning.repo.db_path) as db:
        marker = db.execute("SELECT payload FROM planning_firm_daily WHERE day='__schedule__'").fetchone()
        if marker is None:
            first = now.replace(hour=8, minute=30, second=0, microsecond=0)
            if now.hour >= 12 and (now.hour, now.minute, now.second, now.microsecond) > (12, 0, 0, 0):
                first += timedelta(days=1)
            while first.weekday() >= 5:
                first += timedelta(days=1)
            db.execute("INSERT OR IGNORE INTO planning_firm_daily(day,started_at,status,payload) VALUES('__schedule__',?,'scheduled',?)",
                       (now.isoformat(), json.dumps({'first_run_at': first.isoformat()})))
        marker = db.execute("SELECT payload FROM planning_firm_daily WHERE day='__schedule__'").fetchone()
    first_run = datetime.fromisoformat(json.loads(marker[0])['first_run_at'])
    if now < first_run:
        return {'status': 'outside_window', 'first_run_at': first_run.isoformat()}
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
        payload['verified_people_keys'] = sorted(set(after['verified_people_keys']) - set(before['verified_people_keys']))
        payload['counters'] = {k: max(0, after[k] - before[k]) for k in METRICS}
        planning.repo.execute('UPDATE planning_firm_daily SET completed_at=?,status=?,payload=? WHERE day=?',
                              (planning.outreach.now().isoformat(), status, json.dumps(payload), day))
    return {'status': status, **payload}


def normalize_counters(counters):
    result = {k: counters.get(k, 0) for k in METRICS}
    result['named_people_any'] = counters.get('named_people_any', counters.get('named_people', 0))
    result['est_cost_gbp'] = round(result['serp_calls'] * 0.01, 2)
    result['apollo_credits'] = result['reveals']
    return result


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
    today['counters'] = normalize_counters(today.get('counters', {}))
    totals = {k: sum(normalize_counters(json.loads(r['payload']).get('counters', {})).get(k, 0) for r in rows) for k in METRICS}
    from planning.firm_trial import firm_report, counts_toward_target, person_key
    report = firm_report(repo)
    current = set(report['verified_people_keys'])
    window_keys = set()
    today_keys = set()
    for row in rows:
        keys = set(json.loads(row['payload']).get('verified_people_keys', [])) & current
        window_keys.update(keys)
        if row['day'] == day.isoformat():
            today_keys.update(keys)
    # Legacy runs predate target-key accounting. Re-evaluate their actual records;
    # never reinterpret the old named_people count as verified people.
    for firm in report['firm_records']:
        attempted = firm.get('attempted_at')
        if not attempted:
            continue
        attempted_day = datetime.fromisoformat(attempted.replace('Z', '+00:00')).astimezone(UK).date().isoformat()
        keys = {person_key(firm, p) for p in firm['payload'].get('people', []) if counts_toward_target(firm, p)}
        if attempted_day in days:
            window_keys.update(keys)
        if attempted_day == day.isoformat():
            today_keys.update(keys)
    totals['verified_people'] = len(window_keys)
    today['counters']['verified_people'] = len(today_keys)
    return {'enabled': os.getenv('PLANNING_FIRM_TRIAL_SCHEDULE_ENABLED', '').lower() == 'true',
            'today': today, 'five_day_totals': totals, 'people_target': 100, 'target_window': 'five_weekdays',
            'people_remaining': max(0, 100 - totals['verified_people']), 'days': days, 'today_label': 'Today (UK)', 'five_day_totals_label': 'Last five weekdays (UK)',
            'target_metric': 'unique_verified_people', 'legacy_attribution': 'Current qualification rechecked; legacy records attributed to firm attempted_at (qualification time unavailable)', 'cost_basis': 'SerpAPI estimated GBP 0.01/search; Apollo usage in credits',
            'current_inventory': {k: report[k] for k in METRICS}}


def export_contacts(repo, output):
    from planning.firm_trial import firm_report, counts_toward_target
    fields = ['firm', 'person', 'role', 'email', 'website', 'CH number', 'project refs + addresses', 'evidence URLs', 'review status', 'counts_toward_target']
    with open(output, 'w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for firm in firm_report(repo)['firm_records']:
            data = firm['payload']
            projects = repo.rows('SELECT p.* FROM planning_projects p JOIN planning_firm_projects l ON l.project_id=p.id WHERE l.firm_key=?', (firm['firm_key'],))
            for person in data.get('people', []) + data.get('field_provenance', {}).get('people_candidates', []):
                writer.writerow(dict(zip(fields, [firm['company'], person.get('name'), person.get('role'), person.get('email') or person.get('email_candidate'), data.get('website') or data.get('field_provenance', {}).get('website_candidate'), firm['company_number'],
                    '; '.join(str(p['reference']) + ' | ' + str(p['address'] or '') for p in projects),
                    '; '.join(filter(None, [data.get('ch_source'), person.get('source_url')] + [p.get('source_url') for p in data.get('website_evidence', {}).get('pages', [])] + [p['source_url'] for p in projects])), data.get('review_status', 'trial_review'), counts_toward_target(firm, person) and person in data.get('people', [])])))
