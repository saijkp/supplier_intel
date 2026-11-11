# Planning projects and architect outreach (Phase 2)

Migration 46 adds planning projects, source observations, project-bound enrolment
snapshots, sync runs and historical first-touch exclusions. No send path was added.
The Projects workspace enrols a recorded architect contact into an **unapproved**
sequence; Sai reviews the exact templates and contact list in Outreach. Phase-1
corporate subscriber/address checks, suppression checks, caps and send flags apply.
Developer/applicant variants are available through the same project enrol endpoint
with `role: "developer"`. Subscriber type is never inferred from a planning name.

## First batch

The first batch (kept outside this public repo because it holds contact emails; pass its local path with `--batch`) contains the 100 register projects from the supplied
28 October workbook and 74 email-contact associations explicitly labelled as
applicant or agent routes in its Contacts sheet. Emails remain unreviewed;
register labels and connected-business matches still need identity review.
Inferred supply packages and old email drafts were not promoted into source facts.
The three addresses in `Planning-Leads-SENT-LOG.md` were cross-checked against
`claude-planning-batch1.json`. Address, domain and practice-name exclusions block
fresh enrolments through both the planning and generic outreach endpoints. These
are historical first-touch exclusions, not claims of an opt-out. Follow up only
through a separately reviewed process after checking the actual reply history.

Import explicitly into an isolated local database:

```
python -m planning.import_batch --db output/planning-first-batch.db
```

The importer requires a destination and rejects the configured application DB.
It neither calls the production API nor creates approvals/enrolments. Running
it again merges projects on `(authority, reference)` and deduplicates contacts.
The source workbook remains unchanged. No production import has been performed.

## PlanIt and scheduling

The private legacy repositories were inaccessible through this environment's
network proxy. The fallback client uses the supplied operational lessons plus
the official [API reference](https://www.planit.org.uk/api/) and
[data dictionary](https://www.planit.org.uk/dictionary/): `index` pagination,
90-day windows (up to 180 days per sync), company fields in `other_fields`, and
at least 300 seconds between retries after HTTP 429 (longer numeric Retry-After
values are honoured). Timeouts, HTTP failures, malformed or truncated pagination
are inconclusive errors, never an empty successful result. No live sync was run.
PlanIt may omit agent/applicant companies; these stay blank. Redacted personal
name availability markers are never used to create accounts.

`PLANNING_SCHEDULE_ENABLED=true` opts the single-replica Railway process into a
weekly sync alongside the existing daily outreach routine. Default areas cover
32 London boroughs, City of London and the ten Greater Manchester councils.
Override with `PLANNING_AREAS` as a JSON array of PlanIt authority names/IDs. A
recorded attempt prevents another scheduled attempt for seven days, including
inconclusive runs. Inspect `/planning/runs` and explicitly retry failed areas;
the worker does not spin or erase previous results on an error. Scheduling is off
by default and no deployment/environment settings were changed.

Authenticated endpoints:

- `GET /planning/projects?days=7`: new fit projects, linked accounts and architect contacts.
- `POST /planning/projects/{id}/enrol`: `contact_id`, `mailbox`, optional `role`.
- `POST /planning/sync`: explicit `areas` and `days` (1–180).
- `GET /planning/runs`: recent runs and per-authority errors.

Fit is deterministic and conservative. Reasons quote literal application text;
unit counts require digits followed by residential unit words. No units are
inferred from bedrooms. Unsupported word-number patterns stay unclassified.
Excluded application categories take precedence. Name matching is conservative:
Companies House number, domain, then a unique normalised company label. No paid
company/contact lookup is performed.

Existing non-empty register and account facts are preserved. Changed observations
are stored separately as candidates; the displayed status is the captured status,
not a promise that it is current. Project text is frozen into an enrolment snapshot,
so a later sync cannot alter an approved opener. Contact/account provenance and
corporate eligibility still require review before any email can send.

## Verification and handoff

Offline tests cover pagination, 429 back-off, inconclusive failures, fit evidence,
matching, migration, repeated import, source snapshots, historical first-touch
blocks, authenticated routes, and real JavaScript with a fake DOM/API. Run:

```
python -m pytest -q --ignore=output
```

This Windows sandbox needs a test-process-only `os.mkdir(0700)` to `0777` shim
because newly created private temporary directories cannot be read. The shim and
logs are under ignored `output/`; application code does not change OS permissions.
Ignore the sandbox-generated `pytest-cache-files-*` directories if collection
cannot read them. These are not test sources.

No commit, push, PR, production call, real send or paid call is part of this
handoff. Claude review remains for the owner/reviewer environment before a PR.

### Result in this worktree

- Latest acceptance run: **74 passed** (planning, phase-1 outreach and both JavaScript checks).
- Updated schema expectation checks: **3 passed**.
- Full run with `--ignore=output`, four workers and the temporary-directory shim:
  **2705 passed, 303 failed** in 796.76 seconds. This run collected tests before
  the final schema expectations/frontend fixture updates; those were covered by
  the later focused runs above. The full-suite gate is **not green**.
- Most failures are sandbox filesystem restrictions on the default `tldextract`
  cache and Windows browser subprocess pipes. Representative batch-service and
  real-browser failures reproduced on an untouched HEAD archive under `output/baseline`.
- Full output: `output/phase2-final-pytest.log`; final focused output:
  `output/planning-acceptance.log`; schema checks: `output/version-checks.log`;
  baseline confirmation: `output/baseline-checks.log`.

The local import contains 100 projects, 63 distinct contacts (74 source
associations), and zero enrolments. No commit was made because the full-suite
gate is not green. Re-run the suite in an environment with a writable TLD cache
and working browser subprocess permissions, then obtain Claude review before PR.
