# Website and email enquiry intake

The existing customer inbox worker imports mail every CUSTOMER_INBOX_POLL_MINUTES
(default 30). It creates review cards, never sends mail or automatically converts
unknown customers. Sai adds or dismisses cards in Customers.

## Notification contract and configuration

The sourcing-brief form is a Netlify Form on the IK Eng website (not in this
repository). The real notification for Mahmood's submission (Zoho message
1793311833235005600, 29 Oct 2026, from an earlier mailbox audit) was sent from
`formresponses@netlify.com` to sai@ikeng.co.uk with the subject
`Form submission from sourcing-brief form:`; the body starts directly at the labelled
fields (`Lead Source: ... Full name *: ...`). HTML-only MIME parts are converted to
text by the existing inbox code. Recognised notifications must match:

- CUSTOMER_FORM_BODY_MARKER (default `Form submission from sourcing-brief form:`),
  found in the subject or the body
- CUSTOMER_FORM_SUBJECT_PATTERN (default `(?i)(sourcing[- ]brief|form submission)`)
- CUSTOMER_FORM_SENDERS, when set: comma-separated exact notification addresses.

Recommended: set CUSTOMER_FORM_SENDERS=formresponses@netlify.com on Railway.
The matcher identifies a format, not an authenticated sender; these remain untrusted
review cards. No production settings were changed. Existing Zoho credentials and a
positive poll interval are required. Do not create new credentials for this feature.

Fields are extracted by label, without AI or paid APIs. Original form text and all
parsed values stay on the review card. Missing/invalid customer email remains visible
for manual review and blocks conversion; a missing name requires a name at conversion.
Repeat Message-IDs (or identical raw bytes when missing) are deduplicated for email.
Form notifications also deduplicate by full body hash across redelivery Message-IDs. Separate
submissions from the same address remain separate cards until conversion. Conversion
reuses an existing customer/open enquiry and only fills empty profile/qualification
values. Forms for an already-open enquiry are logged there immediately, with full
text and normal inbound-reply cancellation behavior. Conflicting values stay in the
email evidence and never replace filled values.

Ordinary email intake retains its existing review/dismiss behavior. Free-mail sender
localparts do not trigger the newsletter-name heuristic; explicit bulk/list headers,
auto-replies and supplier/RFQ filtering still apply.

Schema migration 40 adds form_fields and full_text to existing inbox leads. Existing
cards remain readable and can use their stored excerpt when no full text exists.

## Direct Netlify outgoing webhook (preferred)

`POST /customers/form-webhook` accepts the outgoing submission object directly.
It uses no shared API token. NETLIFY_FORM_WEBHOOK_SECRET is mandatory (unset/blank:
503). Missing/invalid JWS, wrong issuer, expired/not-yet-valid temporal claims, or
raw-body hash mismatch returns 401. Only HS256 is accepted, with constant-time
MAC and body-hash comparisons. Netlify documents iss and sha256, not a mandatory exp;
expiry is checked when supplied. The secret must be independent of API_ACCESS_TOKEN.

Body size is capped at 64 KiB while streaming (413). The per-process rate limit is
60 requests/minute/peer IP (429), with bounded storage. It uses the ASGI peer address,
not arbitrary forwarded headers; a shared reverse-proxy peer can share the limit.
This and intake serialization follow the application's single-instance limitation.

NETLIFY_FORM_NAMES is a comma-separated allowlist, default sourcing-brief. A different
form, filled bot-field, or explicit event other than submission_created returns
200 ignored. Netlify's outgoing body is the submission itself, not an event envelope;
the hook configuration selects submission_created. An optional X-Netlify-Event header
or event field is checked when supplied; neither is required for documented form
notification payloads. Missing submission ID is 400. Responses contain status only,
never customer details. No outgoing messages or automatic enquiry creation occur.

The existing email and webhook paths both call store_form_submission. Unknown/new
customers (including known customers with no open enquiry) get the same review cards.
Existing open enquiries get inbound activity, cancellation of pending follow-ups and
blank-only profile/qualification fills. The original webhook JSON stays in full_text
or the activity summary, including fields without a structured destination.

### Verified contract and field mapping

Verified against [Netlify signature docs](https://docs.netlify.com/deploy/deploy-notifications/#payload-signature),
[form notification docs](https://docs.netlify.com/manage/forms/notifications/),
[Netlify staff's outgoing payload example](https://answers.netlify.com/t/what-does-a-netlify-webhook-look-like/973/2),
[Netlify staff's form hook creation example](https://answers.netlify.com/t/api-to-set-webhook-for-forms/36082/2),
and the [API specification](https://github.com/netlify/open-api/blob/master/swagger.yml).
The JWS is X-Webhook-Signature, HS256 with iss=netlify and sha256 equal to the
hex SHA-256 of the exact raw request bytes. The outgoing object contains id,
form_name, form_id, data (raw input names), human_fields (label/value object), and
ordered_human_fields (name/title/value entries). Prefer raw data, even when blank;
use exact known labels/names only as fallbacks. Top-level inferred name/email,
summary and body are not used for extraction.

Exact HTML input names were read from https://ikeng.co.uk/ on 8 November 2026.
Live submission inspection was unavailable: Netlify CLI could not update its local
config under the sandbox; the connector required unavailable approval. No real
customer records were copied into fixtures or committed files.

| Website / data key | Shared form_fields key |
|---|---|
| name, email, phone, company, location | Same names |
| cat (array, JSON array string, or single value) | categories (list) |
| finish_level | finish_level |
| description | requirement |
| design_support | design_support |
| delivery_date | required_date |
| budget, references, materials, dimensions | Same names |
| notes | anything_else |
| lead_source, project_name | Same names |
| role, units, ptype | Preserved in original JSON only |
| form-name / form_name | Form allowlist, not customer data |
| bot-field | Honeypot rejection, not customer data |

Blank/malformed fields stay blank; invalid email remains a review card that cannot
be converted until manually resolved. No AI, external calls or paid extraction.

### Deduplication across email and webhook

Webhook external_id is cust:netlify:<submission id>. Migration 41 adds
customer_form_receipts to remember processed IDs, including IDs acknowledged as
cross-transport duplicates. Existing email-body hashes and legacy leads/activities
are still recognized. Distinct IDs on the same transport are retained.

When a notification lacks a submission ID, compare a canonical fingerprint of all
shared parsed fields (email lowercased, category order normalized) against the
other transport only. This prevents a forwarded notification with the same parsed
values from creating a second card or inbound activity, including after conversion
or dismissal. A changed/truncated/forwarder-edited notification cannot reliably be
identified without its ID; two identical separate submissions across transports
are indistinguishable by fields alone. For reliable ID deduplication regardless of
email formatting, set the email notification subject (if email forwarding is later
enabled) to:

`Form submission from sourcing-brief form: [netlify:%{submissionId}]`

The email branch recognizes that exact ID marker and uses the same external_id.
No email notification settings have been changed by this implementation.

### Post-merge setup (Sai/Claude applies; not performed by this PR)

1. Deploy the merged code to the existing Railway service. Startup applies migration
   41 automatically. Production API: https://supplierintel-production.up.railway.app
   (also recorded in DEPLOY.md).
2. Generate a strong random secret and set Railway NETLIFY_FORM_WEBHOOK_SECRET to it.
   Leave NETLIFY_FORM_NAMES unset for sourcing-brief, or explicitly set sourcing-brief.
   Redeploy/restart so the process loads the variables. Never put the secret in git,
   frontend code, a PR, or API_ACCESS_TOKEN.
3. For site lustrous-torte-afe51b (5e91a71a-0d03-416a-9aae-c71b83ea850c), create a
   Netlify outgoing hook via POST /api/v1/hooks?site_id=5e91a71a-0d03-416a-9aae-c71b83ea850c
   (CLI operation createHookBySiteId), with this hook body. Replace the placeholder
   with the same secret, privately:

```json
{
  "type": "url",
  "event": "submission_created",
  "form_id": "6a987868bb07e60008e0b59f",
  "data": {
    "url": "https://supplierintel-production.up.railway.app/customers/form-webhook",
    "signature_secret": "<same strong random secret as Railway>"
  }
}
```

4. After separate authorization, submit one controlled sourcing-brief and check that
   exactly one Customers review card appears (or one inbound activity for an existing
   open enquiry). Check hook delivery errors in Netlify if it does not. Do not replay
   historical submissions without separate authorization.

No Netlify/Railway settings, production data or notification recipients were changed.
Historical submissions are not backfilled by deploying this change.
