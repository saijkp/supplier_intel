# Planning architect rollout (after merge)

No command below approves a sequence or sends email. Run from the Railway-linked
repository root after Sai merges the PR and the new version is deployed. This is
an in-process BackgroundTasks job, like the existing pipeline pattern: one API
instance only, no durable queue or automatic retry after restart.

```powershell
python scripts/si.py get planning/runs
python scripts/si.py get "pipeline/jobs?limit=50"
python scripts/si.py get planning/runs/3
```

The watchdog runs every minute even when weekly planning sync is disabled; it
also runs on status reads. An unfinished run older than 45 minutes is marked
failed, including legacy run 3. Do not start a second sync while a current run
is unfinished. Each sync has a 40-minute processing budget; requests already in
flight remain bounded by their HTTP timeouts. There is no 300-second 429 sleep.

```powershell
$state = "$env:USERPROFILE\Desktop\IK ENG Customer Enquiries\planning-outreach-rollout.json"
python scripts/planning_rollout.py --state "$state" sync --region inner-london
python scripts/planning_rollout.py --state "$state" sync --region greater-manchester
```

The first command runs Camden, Hackney, Islington, Westminster, Lambeth and
Southwark sequentially. The second runs all ten Greater Manchester boroughs.
Both use 90 days, validated/decided statuses and minimum four residential units.
They stop on an inconclusive PlanIt run. Council identity errors are separately
reported under `identity_errors`; stored applications are not proof of qualified
architects. Inspect them before scaling beyond the initial Camden trial.

PlanIt date parameters are AND filters. The client unions independent
`start_date/end_date` and `decided_start/decided_end` queries, deduplicates by
reference, then checks validation OR decision dates locally. Source:
[PlanIt API](https://www.planit.org.uk/api/).

Council HTTP reads use public HTTPS government hosts, two-second host pacing,
bounded redirects and up to two details links. Successful and failed identity
reads are cached for seven days. 403s are inconclusive; no access controls are
bypassed. Idox Further Information links and NEC/Northgate labelled table,
definition-list and text-line fields are supported. JS-only or changed portal
layouts require manual evidence; mocked layouts are not a live portal warranty.

For a blocked council or a trading name whose profession is not stated, inspect
the official record and the firm's own website. Submit exact evidence through
`POST /planning/projects/{id}/identity`:

```json
{
  "source_url": "https://official-source.example/about",
  "source_text": "EXACT TEXT READ FROM THE SOURCE",
  "values": {
    "agent_company": "EXACT COMPANY VALUE IN SOURCE TEXT",
    "role_evidence": "EXACT ARCHITECT / ARCHITECTURE PHRASE IN SOURCE TEXT",
    "agent_website": "EXACT WEBSITE VALUE IN SOURCE TEXT"
  }
}
```

Every supplied value must occur verbatim in `source_text`. This endpoint is an
authenticated evidence import, not independent validation of user-supplied text.
It preserves filled project fields; conflicting observations remain candidates.
It never equates an arbitrary agent with an architect. Newmark, Lichfields, DP9,
Turley, Savills and Q Square are conservatively treated as planning consultants.
Other unproven roles remain unknown. Applicants remain recorded as applicants;
developer enrolment is refused by the planning endpoint.

Only an exact, unambiguous active Companies House Ltd/LLP profile supplies
corporate verification. Brand-to-legal-name ambiguities require review; the code
does not invent a legal identity. A missing API key leaves the account unverified.
Existing filled account classifications are preserved; a previously flagged or
unknown account needs an evidence-backed explicit review before it is eligible.
Domains must come from source evidence, not company-name guesses. Existing
historical first-touch exclusions and suppressions continue to apply.

```powershell
python scripts/si.py get "planning/projects?days=3650"
python scripts/planning_rollout.py --state "$state" search
python scripts/planning_rollout.py --state "$state" reveal-preview --cap 60
```

Free search is limited to linked, corporate, unflagged architect firms with an
explicit domain; titles are director and associate. Before revealing, review the
printed calls and maximum credit cost. Sai's task authorises at most 60 reveals;
if the preview exceeds that scope, stop. Keep the same state file across retries
and continuations. Do not create a new state file to reset the cumulative cap.

```powershell
python scripts/planning_rollout.py --state "$state" reveal --approved-cap 60
python scripts/planning_rollout.py --state "$state" verify-emails
python scripts/planning_rollout.py --state "$state" render --limit 10 --output "$env:USERPROFILE\Desktop\IK ENG Customer Enquiries\PLANNING OUTREACH - first 10 for approval.txt"
```

Reveals are in batches of at most ten and are persisted as attempted before the
request. They stop on the first failure. No uncertain batch is automatically
retried. `verify-emails` only reviews an unreviewed email when the stored Apollo
response explicitly says verified and it exactly matches the firm's corporate
domain. Other contacts remain unreviewed. No filled name or email is replaced.
Rendering enforces the planning gates, includes project reference/address and
the real footer, and rejects messages over 120 words. It creates no enrolment,
approval or send. If fewer than ten contacts qualify, it writes the actual count
and a blocker instead of inventing recipients.

The first live two-row Camden detail trial on 13 November 2026 returned 403 for
both records. Fixing parsers does not remove that access limitation. No named
architect uplift from those live pages has been demonstrated before deployment.
