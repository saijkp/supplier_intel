"""Migration 50: isolated review identities and persistent request budgets."""
COLUMNS = ('agent_lookup_attempted_at', 'agent_lookup_status')
SQL = """
CREATE TABLE IF NOT EXISTS planning_agent_trials (
 project_id INTEGER PRIMARY KEY REFERENCES planning_projects(id),
 status TEXT NOT NULL DEFAULT 'trial_review' CHECK(status='trial_review'),
 payload TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planning_agent_http (
 host TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
 last_at REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(host,day));
"""
