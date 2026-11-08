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

## Website deployment

No website changes or public endpoint were added: the sourcing-brief host and actual
form transport could not be verified/tested. Keep routing its notification emails
to the configured Zoho inbox. A direct-post integration would require access to that
website, a tested payload contract, and a public endpoint with honeypot/rate limiting.

Historical notifications outside the worker's recent-mail window will not be
backfilled by deploying this change. Review/backfill needs separate authorization.
