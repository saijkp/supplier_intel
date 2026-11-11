"""Explicit local-only import. Never uses the production API or default DB."""
import argparse
import json
from pathlib import Path
from planning.service import PlanningService, name_key
from outreach.repository import OutreachRepository
from outreach.service import OutreachService
from storage.database import initialise_schema


def import_batch(service, batch):
    for identifier in batch['first_touch_exclusions']:
        service.repo.execute('INSERT OR IGNORE INTO planning_first_touch_exclusions VALUES(?,?)',
                             (identifier, '28 Oct 2026 owner sent log and batch-1 drafts'))
    for row in batch['projects']:
        pid = service.store(row)
        contact = row.get('contact')
        if contact and contact.get('email') and contact.get('role') in ('architect','developer'):
            p = service.repo.rows('SELECT * FROM planning_projects WHERE id=?',(pid,))[0]
            aid = p['agent_account_id' if contact['role']=='architect' else 'applicant_account_id']
            if aid:
                account = service.repo.get('prospect_accounts',aid)
                service.outreach.import_rows([{'company':account['company'], 'segment':account['segment'],
                    'contact_name':contact.get('name'), 'email':contact['email'], 'title':contact.get('title'),
                    'source_url':contact.get('source_url')}], 'planning_register', row['source_url'])
    return {'projects':len(batch['projects']), 'first_touch_exclusions':len(batch['first_touch_exclusions'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='Explicit local SQLite destination')
    # The batch holds named contacts' emails, so it lives outside this public repo.
    parser.add_argument('--batch', required=True, help='Local batch JSON (kept outside the repo; contains contact emails)')
    args = parser.parse_args()
    path = Path(args.db).resolve()
    from config.settings import DB_PATH
    if path == Path(DB_PATH).resolve():
        parser.error('Use a separate local database, never the configured application database')
    initialise_schema(path)
    service = PlanningService(OutreachService(OutreachRepository(path), send_enabled=False))
    print(json.dumps(import_batch(service,json.loads(Path(args.batch).read_text(encoding='utf-8')))))


if __name__ == '__main__':
    main()
