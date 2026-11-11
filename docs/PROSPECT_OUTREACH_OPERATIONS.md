# Prospect outreach operations

No production settings/data changed. Sending, scheduling and paid reveal default off.
See [the plan](PROSPECT_OUTREACH_PLAN.md) for targets, compliance and message policy.

## Sai: first campaign

1. Claude reviews, tests and deploys normally, using one Railway replica and the
   existing persistent SQLite volume. Migration 45 is additive. Nothing was pushed.
2. Set `OUTREACH_ZOHO_REGION`, `OUTREACH_ZOHO_CLIENT_ID`, `OUTREACH_ZOHO_CLIENT_SECRET`,
   `OUTREACH_ZOHO_REFRESH_TOKEN`, `OUTREACH_ZOHO_ACCOUNT_ID` for sj@ikeng.online.
   sai uses existing `RFQ_ZOHO_*`. Leave sending and scheduler disabled initially.
3. Outreach → Import: load either Clerkenwell JSON list and record its source/reference.
   CSV columns: name,title,company,email,linkedin_url,phone,source_url. Optional account
   fields: companies_house_number,website,address,postcode,area,segment,subscriber_type.
   No purchased lists or automated Sales Navigator exports. Import never sends.
4. Review each account's subscriber classification, source, priority and flags. Add
   sourced contacts; verify the exact email is a corporate subscription. Employer
   incorporation alone does not make a personal email corporate. Unknown/sole-trader/
   partnership emails stay skipped. Manufacturer-owned showrooms default low priority.
5. Create a segment cadence, review/edit the templates, enrol exact contact IDs and
   mailbox, record a legitimate-interest assessment and approve as Sai. Claims must
   be grounded; no supplier identities/costs/margins. Disclose fees to the end client.
   Changes to templates/list/address review clear approval. Used templates retain
   historic ledger payloads. Changing a used cadence's structure needs a new sequence.
6. Set `OUTREACH_WARMUP_START=YYYY-MM-DD` to sj's actual first day, then deliberately
   set `OUTREACH_SEND_ENABLED=true`. Pilot with Run now: poll replies then bounded
   due steps. sj warms at 5/10/20/30 sends/day by successive week. All Zoho Sent
   traffic counts. While enabled, all app Zoho sends share the 30/day cap and
   90-second spacing, including customer/RFQ sends. Plain text; no tracking.
7. After the pilot set `OUTREACH_SCHEDULE_ENABLED=true`. The API lifespan starts a
   Railway worker ticking every 60 seconds, weekdays 08:30–17:00 Europe/London,
   polling then processing up to ten steps. Repeated ticks drain within caps/spacing.
   Manual Run now uses the same service/ledger. No laptop or separate cron needed.
8. Send LinkedIn tasks manually using copy buttons and profile/search links, then
   mark done or paste replies. Never automate LinkedIn. Check TPS/CTPS/objections
   before manual calls. Record visits/cards; same-day follow-up drafts still need
   corporate-email review and approval. Pause a contact before replacing its sequence.
9. Work project replies through linked customer enquiries. Interested replies create/
   log records before tasks are shown. Sai decides won/lost; won requires numeric
   GBP value and deposit/PO/signed-quote evidence. Record actual margin only if known.

## Delivery review and limitations

Send ledger reservations precede network I/O. Provider IDs/recipients are verified
in Sent. Any uncertainty pauses the contact, stops the batch and blocks subsequent
prospect sends from that mailbox. Inspect Zoho then use timeline delivery review.
Reconciliation records exact recipient/subject matches or consumes the step without
retry. A fresh approved sequence is required for further contact. No blind retries.

Suppression is permanent; RFQ objections also block prospect email. Every reply,
OOO, bounce or opt-out stops all outstanding contact steps/tasks. Polling reads up
to 200 recent messages/14 days; manually inspect mail after longer outages/high
traffic. Ambiguous multi-enrolment sender matches are not guessed. In-process jobs
have no distributed queue or automatic crash recovery. Unique durable step claims
prevent duplicate runs. Keep one replica. Work budget is 45 seconds plus an in-flight
provider timeout. Uncertain sends survive restarts until reviewed.

## Apollo, AI and API

Free search uses only mixed_people/api_search, max 25 accounts/job, default ten.
Every attempt is marked, including missing configuration/failure. No surname/email/
LinkedIn guessing; obfuscated surname is not stored. Not part of daily scheduling.

Paid reveal is separate. Verify your current plan and configure its maximum call
cost in `OUTREACH_APOLLO_CREDITS_PER_CONTACT`; enable `OUTREACH_APOLLO_REVEAL_ENABLED`
only if intended. `POST /outreach/contacts/reveal/previews` takes contact_ids (max
ten) and returns calls/cost. Show Sai first. Only his per-batch yes authorises
`POST /outreach/contacts/reveal/{id}/approve` with approved_by Sai and the exact
confirmed_credit_cost. Batch is marked attempted BEFORE paid calls; first failure
stops it, no retry. No phone/personal-email reveal. Email stays unreviewed. Tests
use fakes; no real reveal performed. AI personalisation is unavailable until a
separately reviewed explicit feature is funded; no OpenAI/SerpAPI calls exist here.

All /outreach routes require bearer auth; jobs return IDs and GET /outreach/jobs/{id}
returns results. No delete endpoints. CLI requires explicit SQLite path:

```
python scripts/prospect_outreach.py --db tmp/prospects.db import data/prospects/clerkenwell-architects-companies-house-2026-11-11.json --source companies_house
python scripts/prospect_outreach.py --db tmp/prospects.db import data/prospects/clerkenwell-showrooms-directory-2026-11-11.json --source clerkenwell_directory
python scripts/prospect_outreach.py --db tmp/prospects.db metrics
```

Git cannot create this worktree's index.lock; leave changes for Claude to commit.
Suggested split: plan alone; schema/service/config/transport/CLI/tests; API/worker/
operations notes; frontend/README/FRONTEND. Run full suite before every commit.
This Windows Python 3.14 sandbox makes pytest mode-0700 folders unreadable. The
untracked tmp/pytest-runtime/sitecustomize.py maps test mkdir 0700 to 0777 without
changing assertions or production code. Normal review should use the original
prescribed pytest command and never bypass the hook.

## Validation recorded 11 November 2026

- Latest focused service/API/frontend suite: **41 passed** (fake Zoho/Apollo only).
- Existing API/jobs/frontend regression subset: **232 passed**.
- Capability-storage/RFQ API/RFQ service regression files after updating the v45
  expectations: **99 passed**.
- Full prescribed suite, with local temp/cache paths and diagnostic reporting:
  **2,955 passed, 24 failed**. Three failures were old v44 expectations; these were
  fixed and covered by the 99-test rerun. The other 21 are Chromium environment/
  browser tests failing because Windows denies Playwright's subprocess pipe
  (`PermissionError: [WinError 5] Access is denied`). No tests were deselected.
- Inline JavaScript syntax (`node --check`), Python compilation and
  `git diff --check` passed. Browser screenshots could not be produced for the same
  pipe restriction; visual QA remains for Claude in an unrestricted environment.

Small follow-up fixes made during full validation were covered by the latest
focused rerun: durable intake-review tasks on customer-record failure, atomic
opt-out suppression, explicit postcode extraction, existing-card visit dedup and
visible end-of-cadence review tasks. The full suite must be rerun on the final
tree before committing in the review environment; no clean full-suite pass is
claimed here. No real email, paid provider call, production access, push or PR.
