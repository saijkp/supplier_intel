# RFQ workspace

The RFQ workspace is linked from the existing supplier app, dashboard and console.
It uses the existing shared API token (one trusted internal workspace, no per-user
accounts). All four `/rfqs` endpoints require that token and refuse access if it
is missing on the server. RFQ and quote content is never exposed by public routes.

## Try the complete workflow locally

Install `requirements.txt`, then run:

```sh
python scripts/run_rfq_preview.py
```

Open <http://127.0.0.1:8765/workspace/rfq.html>. Enter access token
`rfq-local-preview` and connect. This launcher binds only to this computer,
uses `data/rfq_dashboard_preview.db`, and seeds four clearly fictional sample offers.
It never changes the normal supplier database or calls paid integrations.

1. Select the demo RFQ to see complete, missing-MOQ, material-mismatch and
   missing-delivery-terms examples, including mixed currencies.
2. Create an RFQ with a quantity, unit and `name = value` specification lines.
3. Add a supplier quote. Preserve the original reply, manually enter confirmed
   fields and paste exact source excerpts. Leave unknowns blank.
4. Save to validate. Compare offers horizontally, inspect evidence, review and
   copy the targeted Chinese clarification draft. Nothing sends automatically.
5. Record revised offers as new entries. Reload to confirm persistence; original
   replies and prior offers remain available.

## Deploy a safe full preview

Netlify serves the HTML/JavaScript only. Its PR preview needs a backend running
the same branch; the production API does not gain these routes from a frontend
deploy. For a complete hosted preview, deploy this branch to a separate Railway
service/environment with its own persistent volume, `SUPPLIER_INTEL_DB_PATH`,
and `API_ACCESS_TOKEN`. Use the existing uvicorn start command from `DEPLOY.md`.
Do not share the production database volume. Open that service's
`/workspace/rfq.html` to use frontend and API together without CORS configuration.
For Netlify's frontend preview, add its exact HTTPS origin to the preview API's
`ALLOWED_ORIGINS`, then connect using the preview API address and token.

Schema migration 35 creates `rfqs` and `rfq_workspace_quotes` plus an RFQ index; it leaves
existing supplier tables and features intact. Startup applies migrations using
the existing migration runner. Take the usual database backup before eventual
production rollout. This feature branch must be functionally approved before
merging or deploying it to production.

## Scope and validation

Manual entry and linked outreach reply ingestion are implemented. Currency
conversion and landed-cost calculations are not implemented. An evidence excerpt
must occur in the saved reply; a human
still needs to check that it supports the entered value. A complete offer is not
a guarantee of supplier reliability or matching commercial bases across offers.
The table keeps original currencies, Incoterms, ports, quantities and deviations
visible and never declares a cheapest supplier across incompatible terms.

```sh
python -m pytest tests/test_rfq_api.py tests/test_quote_validation.py -q
python -m playwright install chromium
python -m pytest tests/test_rfq_browser.py -q
```

The browser test creates an RFQ, records four offers through the actual UI,
checks missing fields and Chinese drafts, reloads saved records, checks a mobile
viewport and verifies supplier input is rendered as text. API tests cover auth,
fail-closed configuration, source evidence, deviations, persistence and migration
from a database with existing supplier records.

## Linked outreach and automatic clarification

Link a matching existing campaign, or draft a campaign from the master RFQ for
selected database suppliers. Linking requires confirmation that the free-form
campaign specification matches; quantities and units must match in code.
Initial email requests need individual approval in the dashboard. Assisted
channels remain copy/open/paste workflows.

The **Collect replies & validate now** action queues a persistent job. It reads
matching correspondence from the configured dedicated RFQ mailbox, requires the
sender to match the campaign recipient, and leaves unrelated mail unread and
unstored. The existing PDF/XLSX/CSV/TXT attachment extractor is reused. Replies
received through existing outreach routes and pasted replies enter the same
ingestion process. Partial replies without another price are supported.

Labelled `name: value` replies work without an LLM. With `OPENAI_API_KEY`, one
grounded extraction call (20-second timeout, no retries) is used per new reply.
Each cycle ingests up to ten replies and scans at most fifty mailbox messages.
Evidence must occur in the supplier text and numbers must occur in evidence.
Quoted email history is excluded. Ambiguous tiers, failed extraction, conflicting
values or unreadable attachments stop that supplier's automatic sending and
surface the original reply for human review. Conflicting confirmations are
stored as candidates rather than replacing prior non-empty facts. Use **Record
revised offer** to review and confirm those values; old snapshots remain intact.

Per-RFQ settings allow background inbox collection and, separately, explicit
approval of automatic targeted email follow-ups for the linked supplier list.
The default is off. Set a polling interval (at least five minutes), waiting
period (at least one hour), maximum clarification rounds (one to five), and
authorization expiry (up to thirty days). Defaults are 15 minutes, 24 hours,
three rounds and fourteen days. This approval covers missing-detail reminders
only; initial messages and other campaigns are excluded. Existing drafts can
also be approved and sent individually. The global `RFQ_SEND_ENABLED`, mailbox
configuration, buyer identity, rolling daily limit, suppression, flagged-supplier
and atomic message-claim checks continue to apply.

A server worker checks due policies every minute, so collection continues after
the browser closes. Database leases prevent concurrent cycles; inbox IDs and
ingestion records prevent duplicates across restarts. Every send rechecks the
latest quote and pending replies. Completed quotes, paused or expired policies,
opt-outs and hard bounces stop automatic follow-ups. Failed or ambiguous sends
are never automatically retried. Reaching the round limit requires human review.
Each supplier's current validated offer is shown in the comparison, with older
versions retained underneath. Complete offers still require matching commercial
bases before a purchasing decision.

Migration 36 adds campaign links, ingestion records, policies and clarification
audit records. It preserves all v34 outreach and v35 workspace data. Mailbox
credentials stay in server environment variables, never browser storage or
status responses. The isolated preview still needs its dedicated mailbox
credentials and buyer identity before it can collect or send real email.

### Zoho Free and WhatsApp

Do not assume Zoho Free includes IMAP. A native Zoho Mail REST transport is
available via `RFQ_MAIL_TRANSPORT=zoho`. Obtain account-authorized OAuth access
using Zoho's official process, then set these variables **on the backend only**:
`RFQ_ZOHO_REGION` (`com`, `eu`, `in`, `com.au`, or `jp`, matching the actual
account data center), `RFQ_ZOHO_CLIENT_ID`, `RFQ_ZOHO_CLIENT_SECRET`,
`RFQ_ZOHO_REFRESH_TOKEN`, `RFQ_ZOHO_ACCOUNT_ID`. Keep `RFQ_FROM_ADDRESS`, buyer
identity and the send enablement/caps configured as above. Required OAuth scopes
are `ZohoMail.messages.READ` and, for sending, `ZohoMail.messages.CREATE`.
The configured From address must be authorized on that Zoho account. Never put
passwords or tokens into browser storage, chat, code or PR descriptions.

The API transport refreshes its token server-side, retrieves original MIME mail
for the existing attachment/quote pipeline, and filters unrelated senders and
subjects from metadata before downloading message bodies. It never marks mail
read. Sending is not retried when Zoho fails to confirm acceptance. Account
permissions and provider limits must be checked with the real account before
activation; tests use fake transports and do not prove a live connection.

Use one authorized mailbox for active supplier outreach. Historical mail in a
different account is not automatically mixed into the campaign; paste relevant
supplier replies explicitly. Switching to a new sender requires authorizing that
mailbox and updating server configuration, not changing an address in a draft.

WhatsApp `.txt` chat exports can be imported under a linked supplier message.
Choose the exact supplier sender name. Both Android and iOS timestamp formats
are supported; buyer/system messages are excluded and repeated imports are
deduplicated. Exports are limited to 2 MB, 100 supplier messages and 50,000
characters per individual message. Missing media requires review. WhatsApp
remains assisted: copy the targeted Chinese draft, open the supplier channel,
and import/paste their next reply. Personal WhatsApp collection and sending are
not automated.

Provider contracts:
[Zoho IMAP availability](https://www.zoho.com/mail/help/imap-access.html),
[Zoho OAuth](https://www.zoho.com/mail/help/api/using-oauth-2.html),
[list emails](https://www.zoho.com/mail/help/api/get-emails-list.html),
[original MIME](https://www.zoho.com/mail/help/api/get-original-message.html),
[send API](https://www.zoho.com/mail/help/api/getting-started-with-api.html).

New authenticated endpoints live under `/rfqs/{id}/outreach`: `link`, `create`,
`policy`, `sync`, `messages/{id}/reply`, and `messages/{id}/send`. GET the same
outreach path for progress, message history, draft state and readiness. List
existing campaigns at `/rfqs/outreach/campaigns`. Background sync/send actions
return existing pipeline job IDs.
