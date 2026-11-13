"""Explicit post-merge planning rollout. Never approves sequences or sends mail.

Paid reveals are a separate command, require a prior saved cost preview and an
explicit cap. State is persisted before each reveal so uncertain calls cannot
be retried automatically. Keep the state file (it contains business contacts).
"""
import argparse
import json
from pathlib import Path
import re
import time

from si import call

INNER = ['Camden', 'Hackney', 'Islington', 'Westminster', 'Lambeth', 'Southwark']
GM = ['Bolton', 'Bury', 'Manchester', 'Oldham', 'Rochdale', 'Salford', 'Stockport', 'Tameside', 'Trafford', 'Wigan']


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    temporary.replace(path)


def architects():
    accounts = {}
    projects = call('GET', '/planning/projects?days=3650')
    for project in projects:
        aid = project.get('agent_account_id')
        if project.get('agent_role') != 'architect' or not aid or aid in accounts:
            continue
        account = call('GET', f'/outreach/accounts/{aid}')
        if account['subscriber_type'] == 'corporate' and account['domain'] and not account['flagged']:
            accounts[aid] = (account, project)
    return accounts


def candidates(accounts):
    for account, project in accounts.values():
        for contact in account['contacts']:
            if not contact['name'] or not re.search(r'\b(?:director|associate)\b', contact['title'] or '', re.I):
                continue
            if contact['opted_out_at'] or contact['bounced_at']:
                continue
            yield account, project, contact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    sub = parser.add_subparsers(dest='phase', required=True)
    sync = sub.add_parser('sync'); sync.add_argument('--region', choices=['inner-london', 'greater-manchester'], required=True)
    sub.add_parser('search')
    preview = sub.add_parser('reveal-preview'); preview.add_argument('--cap', type=int, choices=range(1,61), default=60)
    reveal = sub.add_parser('reveal'); reveal.add_argument('--approved-cap', type=int, choices=range(1,61), required=True)
    sub.add_parser('verify-emails')
    render = sub.add_parser('render'); render.add_argument('--limit', type=int, default=10); render.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.state.read_text(encoding='utf-8')) if args.state.exists() else {}
    if args.phase == 'sync':
        for area in (INNER if args.region == 'inner-london' else GM):
            job = call('POST', '/planning/sync', {'areas':[area], 'days':90, 'statuses':['validated','decided'], 'min_units':4})
            state.setdefault('runs', []).append({'area':area, 'id':job['id']}); save(args.state, state)
            while True:
                run = call('GET', job['status_url'])
                if run['completed_at']:
                    result = json.loads(run['result']); print(area, json.dumps(result))
                    if not result.get('complete'):
                        raise SystemExit('Stopped on inconclusive run; inspect /planning/runs before resuming')
                    break
                time.sleep(10)
        return
    accounts = architects()
    if args.phase == 'search':
        for aid in accounts:
            print(aid, call('POST', f'/planning/accounts/{aid}/contacts/search'))
        return
    contacts = list(candidates(accounts))
    if args.phase == 'reveal-preview':
        if state.get('reveal_previews'):
            raise SystemExit('Existing reveal previews retained; inspect state instead of replacing them')
        remaining = args.cap - state.get('attempted_reveals', 0)
        ids = list(dict.fromkeys(c['id'] for a,p,c in contacts if not c['email'] and not c['reveal_attempted_at'] and c['source']=='apollo_free_search'))[:max(0,remaining)]
        previews = state.setdefault('reveal_previews', [])
        for start in range(0, len(ids), 10):
            batch = call('POST', '/outreach/contacts/reveal/previews', {'contact_ids':ids[start:start+10]})
            previews.append(batch); save(args.state, state)
            print(json.dumps(batch))
        print('Contacts:', len(ids), 'maximum credits:', sum(b['maximum_credit_cost'] for b in previews))
    elif args.phase == 'reveal':
        previews = state.get('reveal_previews', [])
        if not previews:
            raise SystemExit('Run reveal-preview first and review exact credit cost')
        pending = [b for b in previews if not b.get('attempted')]
        if state.get('attempted_reveals', 0) + sum(b['calls'] for b in pending) > args.approved_cap:
            raise SystemExit('Approved reveal cap exceeded')
        for batch in pending:
            print('Revealing', batch['calls'], 'contacts; maximum credits:', batch['maximum_credit_cost'], flush=True)
            batch['attempted'] = True
            state['attempted_reveals'] = state.get('attempted_reveals', 0) + batch['calls']; save(args.state, state)
            result = call('POST', '/outreach/contacts/reveal/'+batch['id']+'/approve', {'confirmed_credit_cost':batch['maximum_credit_cost'], 'approved_by':'Sai'})
            batch['result'] = result; save(args.state, state)
            if result['status'] != 'completed':
                raise SystemExit('Reveal stopped; do not retry uncertain calls')
    elif args.phase == 'verify-emails':
        for account, project, contact in contacts:
            email = contact['email'] or ''
            if email.rpartition('@')[2].lower() != account['domain'].lower() or not email:
                continue
            # Only the provider's explicit verified signal, never just an MX
            # result or a guessed name/domain pattern, permits this review.
            evidence = [e for e in account['events'] if e['kind']=='paid reveal']
            verified = any((v:=json.loads(e['payload'])).get('contact_id') == contact['id'] and
                           (v.get('candidate') or {}).get('email_status') == 'verified' and
                           (v.get('candidate') or {}).get('email', '').lower() == email.lower() for e in evidence)
            if verified and contact['email_status']=='unreviewed':
                print(contact['id'], call('PATCH', f"/outreach/contacts/{contact['id']}", {'email_status':'corporate_verified'}))
    elif args.phase == 'render':
        previews = []; failures = []; seen = set()
        for account, project, contact in contacts:
            if len(previews) >= args.limit: break
            if contact['id'] in seen or contact['email_status'] != 'corporate_verified': continue
            seen.add(contact['id'])
            try:
                previews.append(call('POST', f"/planning/projects/{project['id']}/preview", {'contact_id':contact['id']}))
            except SystemExit as exc:
                failures.append(str(exc))
        text = f'{len(previews)} verified drafts for Sai approval. No sequences approved; no mail sent.\n\n'
        text += '\n\n'.join(f"To: {p['recipient']}\nSubject: {p['subject']}\nWords: {p['words']}\n\n{p['body']}\n\nCouncil: {p['project_hook']['source_url']}" for p in previews)
        if len(previews)<args.limit:
            text += '\n\nBLOCKED: fewer eligible verified contacts than requested.\n'+'\n'.join(failures)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
        print('Rendered:', len(previews), 'to', args.output)


if __name__ == '__main__':
    main()
