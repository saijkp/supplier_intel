"""Review-only firm identities, including negative attempt markers."""
SQL = """
CREATE TABLE IF NOT EXISTS planning_firm_trials (
 firm_key TEXT PRIMARY KEY, company_number TEXT UNIQUE, company TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'trial_review' CHECK(status='trial_review'),
 attempted_at TEXT, payload TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS planning_firm_aliases (name_key TEXT PRIMARY KEY, firm_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planning_firm_projects (
 firm_key TEXT NOT NULL REFERENCES planning_firm_trials(firm_key),
 project_id INTEGER NOT NULL REFERENCES planning_projects(id),
 PRIMARY KEY(firm_key, project_id));
"""
