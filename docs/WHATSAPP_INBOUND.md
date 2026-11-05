# Meta WhatsApp inbound receiver — Issue #23

Status: optional, disabled by default, test-account-only. No Graph API calls,
access tokens, sending, media downloads, paid extraction, deployments or changes
to Zoho / manual WhatsApp export import. Every inbound message initially requires
manual review; a sender phone number is not evidence of supplier identity.

## Railway setup for a later, separately approved test deployment

Do not change production variables or deploy as part of this PR. Use a separate
Railway test environment with its own persistent SQLite volume and one replica.
Set these privately in Railway's variable editor, never in issues, screenshots,
commands pasted into chat, committed files or logs:

- `WHATSAPP_INBOUND_ENABLED=true` (absent/blank defaults false)
- `WHATSAPP_APP_SECRET`: SupplierIntel Meta app secret (not a Graph access token)
- `WHATSAPP_VERIFY_TOKEN`: new high-entropy random verification secret, identical
  to the value entered privately in Meta's webhook configuration
- `API_ACCESS_TOKEN`: separate private token for the existing admin authentication
- `SUPPLIER_INTEL_DB_PATH`: test environment's persistent database path

Use HTTPS callback `https://<test-service-domain>/webhooks/whatsapp`.
The production-shaped callback would be
`https://supplierintel-production.up.railway.app/webhooks/whatsapp`, but is not
configured or enabled by this work. Do not enter that callback yet.

Use `uvicorn api.app:app --host 0.0.0.0 --port $PORT --no-access-log` for the test
service (Railway expands PORT). Disable proxy request/query/body/header logging
and APM request capture: GET verification contains the verify token in its query.
Application webhook logging contains accepted and duplicate counts only.
Do not enable request debug logging or log environment values.

In Meta Developer dashboard, select **SupplierIntel / Test Messaging Account**
only. Configure the callback and verify token, subscribe to `messages`, and verify
that the app subscription is for test WABA `1713595540331361`, phone number ID
`1383583781512437`. Both IDs are pinned in code; other accounts are rejected even
if correctly signed. Do not select the historical disabled IK Eng LTD account.
No number registration, migration, subscription changes or account calls are
performed by this receiver. Do not register, migrate, disconnect or change the
current WhatsApp Business app number +44 7767 990007.

Use Meta's webhook test delivery or a human-originated message to the displayed
test number. Do not use the API to send a test message. A generic dashboard sample
with placeholder account IDs will be rejected: use a genuine test-account event
or the local signed fixtures. Confirm handshake returns the plain numeric
challenge; missing/forged signatures yield 401, wrong account or malformed payload
400, payload above 256 KiB 413, disabled routes 404, missing secrets 503.
Delivery-status notifications are acknowledged without storing them. Identical
message IDs yield success without a second row. Database failures produce 5xx
rather than acknowledging lost data, allowing Meta retries.

## Review and mapping

Open `/workspace/whatsapp-review.html` on the API's static app (or the same file on the
frontend host), also linked from the RFQ workspace. Supply the test API URL and
admin token; the page holds credentials only in memory. `GET /whatsapp/inbound`
returns up to 100 active records ordered by ID, with `?after=<last-id>` pagination.
All review/mapping endpoints require the existing admin bearer token and feature
flag. Each association uses `PATCH /whatsapp/inbound/{id}` with
`{"supplier_id":123,"rfq_id":"workspace-rfq-id"}`. Confirm the supplier identity
and enquiry before saving. Missing/flagged suppliers and nonexistent RFQs are
rejected. Association does not create or merge contacts, mutate supplier data,
parse a quote, schedule a follow-up, or feed the automated outreach worker.
The review list is read-only with respect to message content; only this explicit
association can be edited. Shared-token admin identity is an existing limitation.

Text up to 4096 characters is retained. Media and other message types store only
the type and a manual-review placeholder; no media URL, caption, contact profile,
attachment ID or raw provider payload is persisted. Sender, provider ID, type and
provider timestamp are stored with local receipt time. Incoming text is untrusted
and displayed as text, never HTML or agent instructions. No automatic matching
is performed, including when multiple suppliers share the same phone.

## Privacy and retention

Use test correspondence only. Private content and sender/association metadata are
cleared after 30 days, on every ingest and review/association operation. With no
traffic, expiry is enforced on the next operation; this is lazy retention, not a
scheduled physical deletion guarantee. Database backups must follow the same
retention policy and need separate operator configuration before real use.
Provider IDs and receipt/type metadata remain as minimal idempotency tombstones
to prevent old deliveries recreating private content. Provider IDs can themselves
be pseudonymous personal data: review their retention before any real rollout.
A future erasure request needs an operator-run, scoped local database operation
and backup handling; no production erasure tooling is introduced here.
Use encrypted storage/backups, restrict admin token access, and have the controller
approve lawful basis, privacy notice, retention and erasure handling before real
supplier data. Unmatched messages are visible only in the private review queue.

## Coexistence manual gate and future outbound phase

The test messaging account does **not** demonstrate Coexistence eligibility for
the current Business app number. This PR intentionally cannot onboard that number.
An operator with direct Meta Business Manager access must first confirm current
app/account/region eligibility, the official Business App onboarding flow,
permissions/app review and limitations displayed by Meta, and confirm that the
phone app stays usable. Stop if the flow requests deletion, migration or ordinary
Cloud API number registration. Never use the disabled account as a workaround.
Real-number support requires separate review of the hard-coded account allowlist,
a separately approved onboarding and deployment decision, and privacy readiness.

Official references for the operator to recheck at onboarding:

- https://developers.facebook.com/docs/graph-api/webhooks/getting-started
  (GET challenge and signed raw POST payload)
- https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
  (messages event envelopes and message types)
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users
  (Coexistence eligibility and limitations)
- https://business.whatsapp.com/policy
  (messaging policy and consent)

Research attempted 2026-11-05: official developer pages returned HTTP 429 or were
unavailable to the research tool. Current detailed eligibility and limitations
therefore remain explicitly unverified; no third-party claims are used as
onboarding authorization. An operator must verify the current official pages and
Meta dashboard before changing a real account.

For a separately scoped future outbound phase, verify current Meta policy:
recipient opt-in and opt-out handling, approved templates when outside the
customer-service 24-hour window, category/rate limits and pricing. Every outbound
send must require explicit buyer approval, strict caps, an audit trail and a
separate deployment decision. No outbound code or access token is provided here.

## Staged rollout / rollback

1. Review draft PR and local offline tests. Keep feature false everywhere.
2. Separately approve isolated test Railway deployment and privately set secrets.
3. Verify only test WABA events, signatures, replay, queue and explicit mapping.
4. Check log redaction and retention/backups; manually confirm Coexistence gates
   before proposing any real-account support in another PR.
5. Roll back intake by setting `WHATSAPP_INBOUND_ENABLED=false` in that test
   environment only. Existing importer and Zoho flows continue unchanged.

Offline tests: `python -m pytest tests/test_whatsapp_webhook.py -q` and required
full suite `python -m pytest -q`. Fixtures use a temporary database and dummy
secrets. No live webhook, paid service or real message send is needed.
