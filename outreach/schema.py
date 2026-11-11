"""Additive outreach schema, shared by fresh installs and migration 45."""
SQL = """
CREATE TABLE IF NOT EXISTS prospect_accounts (
 id INTEGER PRIMARY KEY, company TEXT NOT NULL, segment TEXT NOT NULL,
 website TEXT, domain TEXT, address TEXT, postcode TEXT, area TEXT,
 companies_house_number TEXT UNIQUE, name_key TEXT NOT NULL,
 subscriber_type TEXT NOT NULL DEFAULT 'unknown', source TEXT NOT NULL,
 source_url TEXT, fit_notes TEXT, priority TEXT DEFAULT 'low', stage TEXT DEFAULT 'prospect',
 owner TEXT DEFAULT 'Sai', flagged INTEGER DEFAULT 0, flag_reason TEXT, contact_search_attempted_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_prospect_domain ON prospect_accounts(domain);
CREATE TABLE IF NOT EXISTS prospect_contacts (
 id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES prospect_accounts(id),
 name TEXT, title TEXT, email TEXT, email_status TEXT DEFAULT 'unreviewed',
 linkedin_url TEXT, phone TEXT, source TEXT NOT NULL, source_url TEXT,
 project_hint TEXT, project_hint_source TEXT, opted_out_at TEXT, bounced_at TEXT,
 search_attempted_at TEXT, reveal_attempted_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(account_id,email));
CREATE TABLE IF NOT EXISTS outreach_sequences (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, segment TEXT NOT NULL,
 approved_at TEXT, approved_by TEXT, legitimate_interest_note TEXT,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outreach_steps (
 id INTEGER PRIMARY KEY, sequence_id INTEGER NOT NULL REFERENCES outreach_sequences(id),
 position INTEGER NOT NULL, channel TEXT NOT NULL CHECK(channel IN ('email','linkedin_task','visit_task','call_task')),
 day_offset INTEGER NOT NULL CHECK(day_offset>=0), subject TEXT NOT NULL DEFAULT '',
 template TEXT NOT NULL, UNIQUE(sequence_id,position));
CREATE TABLE IF NOT EXISTS outreach_enrolments (
 id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES prospect_contacts(id),
 sequence_id INTEGER NOT NULL REFERENCES outreach_sequences(id), mailbox TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', next_step_at TEXT NOT NULL,
 started_at TEXT NOT NULL, UNIQUE(contact_id,sequence_id));
CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_active_contact ON outreach_enrolments(contact_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS outreach_events (
 id INTEGER PRIMARY KEY, enrolment_id INTEGER REFERENCES outreach_enrolments(id),
 account_id INTEGER REFERENCES prospect_accounts(id), step_id INTEGER REFERENCES outreach_steps(id),
 kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'done', payload TEXT NOT NULL DEFAULT '{}',
 provider_id TEXT, external_id TEXT UNIQUE, due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(enrolment_id,step_id));
CREATE INDEX IF NOT EXISTS idx_outreach_due ON outreach_enrolments(status,next_step_at);
CREATE TABLE IF NOT EXISTS suppression_list (
 identifier TEXT PRIMARY KEY, reason TEXT NOT NULL, added_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS suppression_no_delete BEFORE DELETE ON suppression_list
 BEGIN SELECT RAISE(ABORT,'Suppression identifiers are permanent'); END;
CREATE TABLE IF NOT EXISTS prospect_enquiry_links (
 account_id INTEGER NOT NULL REFERENCES prospect_accounts(id),
 enquiry_id INTEGER NOT NULL UNIQUE REFERENCES customer_enquiries(id),
 margin_gbp REAL, PRIMARY KEY(account_id,enquiry_id));
CREATE TABLE IF NOT EXISTS outreach_jobs (
 id TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outreach_reveal_batches (
 id TEXT PRIMARY KEY, contact_ids TEXT NOT NULL, credit_cost INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'preview', attempted_at TEXT, result TEXT, created_at TEXT NOT NULL);
"""

# sqlite triggers contain semicolons, so split only at complete statements.
def statements():
    import sqlite3
    pending = ''
    for line in SQL.splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
