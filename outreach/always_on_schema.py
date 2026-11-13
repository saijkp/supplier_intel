"""Durable orchestration state; additive migration 49."""
SQL = '''
CREATE TABLE IF NOT EXISTS outreach_runtime (
 key TEXT PRIMARY KEY, payload TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS outreach_daily_runs (
 day TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT, result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS outreach_project_queue (
 project_id INTEGER NOT NULL REFERENCES planning_projects(id),
 account_id INTEGER NOT NULL REFERENCES prospect_accounts(id), role TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'waiting enrichment', reason TEXT NOT NULL DEFAULT '',
 attempted_at TEXT, retry_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 enrolment_id INTEGER REFERENCES outreach_enrolments(id),
 PRIMARY KEY(project_id,account_id));
CREATE TABLE IF NOT EXISTS outreach_credit_ledger (
 contact_id INTEGER PRIMARY KEY REFERENCES prospect_contacts(id), day TEXT NOT NULL,
 credits INTEGER NOT NULL, attempted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outreach_seed_results (
 id INTEGER PRIMARY KEY, mailbox TEXT NOT NULL, seed TEXT NOT NULL,
 provider_id TEXT, placement TEXT NOT NULL DEFAULT 'pending', confirmed_by TEXT,
 checked_at TEXT NOT NULL, UNIQUE(mailbox,seed));
CREATE TABLE IF NOT EXISTS zoho_token_cache (
 identity TEXT PRIMARY KEY, token TEXT, expires REAL NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, error_category TEXT, lock_owner TEXT, lock_until REAL NOT NULL DEFAULT 0);
'''

def statements():
    return [s for s in SQL.split(';') if s.strip()]
