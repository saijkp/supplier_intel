# Always-on project outreach (Railway)

The API lifespan starts `outreach.worker` every 60 seconds. Its DB lease allows
one daily loop at a time on a shared persistent SQLite volume. Keep one Railway
replica; separate replica volumes are not a shared database. No browser, laptop,
LinkedIn automation, OpenAI completion or scheduled desktop task is involved.

Migration 49 adds the daily run ledger, retry/fix queue, credit reservations,
seed results, runtime leases and private shared Zoho access-token cache. Back up
the persistent volume before deploying. Never export the token-cache table.

Defaults: scheduler on, **dry-run on**, three rotating London/Greater Manchester
authorities per weekday, 90-day overlapping planning fetches, 180-day maximum age
at send time, Apollo maximum 10 credits per UK day, and sj at 5 sends per day.
A failed daily sync retries its same rotation after one hour. Every project has
an identity status or firm-target queue row. Queue overflow remains in the DB.
Cadence offsets are 0/4/9/16/28 days; LinkedIn day 9 is a manual task. Delayed
touches retain the spacing to the next touch. Four emails, five touches total.

Settings (Railway environment):

- `OUTREACH_ALWAYS_ON_ENABLED=true` (false restores opt-in legacy workers).
- `OUTREACH_DRY_RUN=true`: never sends managed project emails or seed tests;
  paid automatic reveals are disabled in dry-run. Free search/sync still run.
- `OUTREACH_SEND_ENABLED=true` is also required for live prospect email.
- `OUTREACH_GLOBAL_PAUSE=true` blocks all prospect outreach, including legacy
  run-due routes. Reply polling and health checks continue.
- `OUTREACH_PLANNING_AREAS`: optional JSON array; default all 33 London areas
  and 10 Greater Manchester authorities. `OUTREACH_BOROUGHS_PER_DAY=3` (1–5).
- `OUTREACH_APOLLO_REVEAL_ENABLED=true` enables reserved automatic reveals;
  `OUTREACH_APOLLO_DAILY_CREDITS=10`; `OUTREACH_APOLLO_CREDITS_PER_CONTACT=1`
  must match the subscribed Apollo plan. Reservations count uncertain attempts
  as spent. Explicit/manual reveal batches share the same daily budget.
- `OUTREACH_MAILBOXES` JSON maps address to `prefix`, `cap`, `warmup_start` (ISO
  date), and weekly `ramp`. Example:
  `{"sj@ikeng.online":{"prefix":"OUTREACH_ZOHO_","cap":5,"warmup_start":"2026-11-13","ramp":[5,10,20,30]}}`.
  Add ikeng.org mailboxes with their own env prefix and Zoho REGION, ACCOUNT_ID,
  CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN. Never use ikeng.co.uk for prospecting.
  The cap is a ceiling; changing the weekly ramp alone never overrides it.
- `OUTREACH_WARMUP_START`: existing sj setting, if present, remains an additional
  ceiling. Clear or align it with the configured mailbox baseline.
- `OUTREACH_SEED_ADDRESSES`: JSON list of owned/authorised seed inboxes.
  `OUTREACH_SEED_SEND_ENABLED=true` permits plain-text seed tests in the UK
  sending window when dry-run is off. Seed sends use Sent checks and spacing.
  New mailboxes get zero prospect capacity until all configured seeds have
  confirmed inbox results. Existing sj may continue at 5; ramp above 5 requires
  seeds too. Manual Sai confirmation is the supported placement fallback;
  provider message acceptance never counts as inbox placement. Results expire
  after 30 days; refresh them using the result endpoint after re-testing.
- `OUTREACH_DKIM_SELECTORS`: JSON domain-to-selector-list map, e.g.
  `{"ikeng.online":["zoho"],"ikeng.org":["zoho"]}`. Use actual provider selectors.
  `python main.py doctor --live` checks SPF/DKIM/DMARC TXT records. This proves
  record presence, not cryptographic delivery authentication or inbox placement.
- Existing `RFQ_ZOHO_*` system mailbox sends health alerts to sai@ikeng.co.uk.

Authenticated endpoints:

- GET `/health/outreach`: heartbeat, reasons, pause/dry-run, mailbox caps/rates.
- GET `/outreach/always-on/status`: daily/weekly summaries, runs and fix queue.
- GET `/outreach/always-on/preview`: up to ten eligible next-day rendered emails.
- POST `/outreach/always-on/template/approve` with `approved_by: Sai`, `note`:
  approves the exact code-defined five-step template once. Wording changes
  invalidate approval at send time. Individual legacy sequences retain their
  existing exact-contact approval rules.
- POST `/outreach/always-on/pause` with `paused: true|false`.
- POST `/outreach/always-on/sequences/{id}/pause` with `paused: true|false`.
- POST `/outreach/always-on/seeds/result` with `mailbox`, `seed`, `placement`
  (`inbox|spam|missing`), `confirmed_by: Sai`.
- POST `/outreach/always-on/jobs`: enqueue a bounded BackgroundTasks run in
  pipeline_jobs, with existing job status/progress routes.

A verified corporate-domain Apollo email or manual verified contact, Companies
House corporate evidence, a named person, a recent fit project with exact address
and proposal, and a live non-parking firm website are mandatory. Long applications
that make the intact email reach 120 words go to the fix queue. No generic fallback.
Missing firm/domain identities remain visible and retry enrichment. Council pages
that need JavaScript or lack labels still need human evidence import; this worker
does not bypass council access controls or invent the identity.

Zoho access tokens are shared by credential fingerprint in SQLite, cached until
expiry, with one refresh owner and a recoverable 90-second lease. Refresh rate
limits back off 5 minutes across processes. Explicit rejected prospect and previously authorised RFQ sends retry with
exponential backoff. Bad recipients move to another decision maker at the firm;
complaints suppress the firm. Ambiguous delivery is held for Sent reconciliation,
never blindly retried. The existing uncertain-delivery UI remains the review path.
Mailbox bounce rates over 3% or complaints halve the configured capacity (minimum
one, when otherwise eligible). Reply and complaint rates use imported mail events;
there is no external ESP complaint webhook or automatic seed-inbox reader.

Today shows health and counts; failures have a red banner and a link to Outreach.
Health alerts are deduplicated per UK day. An entirely stopped Railway app cannot
send its own alert: monitor `/health/outreach` externally for a true dead-man
monitor. Existing Zoho reconnect uses `scripts/zoho_connect.py`; the dashboard
review button does not implement a server OAuth reconnect flow.

Release: Claude stages the explicit file list, runs the full suite (only the two
known GoogleSearchScraper failures allowed), commits through the hook and opens a
separate PR based on fix/planning-architect-outreach. Do not merge. Sai reviews the
first eligible preview, approves the template, merges explicitly, then disables
dry-run in Railway. Live Apollo/provider behavior and actual inbox placement must
be checked on deployment; unit tests use fakes only.
