"""Planning register schema (migration 46)."""
SQL = '''
CREATE TABLE IF NOT EXISTS planning_projects (
 id INTEGER PRIMARY KEY, authority TEXT NOT NULL, reference TEXT NOT NULL,
 agent_lookup_attempted_at TEXT, agent_lookup_status TEXT,
 address TEXT, postcode TEXT, description TEXT, application_type TEXT, units INTEGER,
 status TEXT, validated_date TEXT, decided_date TEXT, applicant TEXT, agent TEXT,
 source_url TEXT, fit_score INTEGER NOT NULL, fit_reasons TEXT NOT NULL,
 applicant_account_id INTEGER REFERENCES prospect_accounts(id),
 agent_account_id INTEGER REFERENCES prospect_accounts(id), first_seen_at TEXT NOT NULL,
 UNIQUE(authority,reference));
CREATE TABLE IF NOT EXISTS planning_project_observations (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES planning_projects(id),
 payload TEXT NOT NULL, observed_at TEXT NOT NULL, UNIQUE(project_id,payload));
CREATE TABLE IF NOT EXISTS planning_enrolment_projects (
 enrolment_id INTEGER PRIMARY KEY REFERENCES outreach_enrolments(id),
 project_id INTEGER NOT NULL REFERENCES planning_projects(id), snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planning_first_touch_exclusions (
 identifier TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planning_sync_runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT, result TEXT);
'''

def statements():
    return [s for s in SQL.split(';') if s.strip()]
