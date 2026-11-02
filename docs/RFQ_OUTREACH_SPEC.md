# RFQ outreach: build spec

Goal: get real quotes back faster. Take a shortlist of suppliers already in the DB, send them a request for quotation over every usable channel, capture replies, extract quotes with evidence, and compare them.

Read `CLAUDE.md` first. Its standing rules apply, especially: grounded extraction only (rule 3), flagged suppliers excluded (rule 8), small test first (rule 6), never blend scores.

## Channel reality (do not try to work around this)

| Channel | Mode | How |
|---|---|---|
| email | automated | SMTP send, IMAP read. Works with any provider (Gmail app password, Outlook, etc.) |
| alibaba | assisted | No buyer-side messaging API. Do NOT script a logged-in account or scrape inquiry forms: it breaches ToS and risks an account ban. Generate the message and a link; a human sends it |
| made_in_china | assisted | Same as Alibaba |
| wechat | assisted | No API for messaging strangers from a personal account. Show `wechat_id` and copy-ready Chinese text |
| whatsapp | assisted | `https://wa.me/<digits>?text=<urlencoded>` click-to-chat link |
| contact_form | assisted | Link to `suppliers.contact_form_url` plus the message text |

Assisted flow: draft, human sends and clicks "mark sent", human pastes the reply, same quote parser runs.

Marketplace links: use `suppliers.alibaba_url` / `made_in_china_url` when present. Otherwise reuse `batch.tracker_exporter._marketplace_search_link`. Never invent deep-link URL formats.

## Safety rules (hard requirements, each needs a test)

1. Nothing sends without an explicit human approval: only rows with status `approved` can send.
2. `RFQ_SEND_ENABLED` defaults to false. When false, a send does nothing and reports why.
3. Sending also refuses unless SMTP config and buyer identity (`RFQ_BUYER_COMPANY`, `RFQ_BUYER_NAME`, `RFQ_BUYER_ADDRESS`) are all set.
4. `RFQ_DAILY_SEND_LIMIT` (default 25) is a rolling 24h cap across all campaigns, counted from the DB.
5. Atomic claim: `approved` to `sending` with a conditional UPDATE before the SMTP call. A row left in `sending` after a crash is never resent automatically; surface it for a human.
6. At send time re-check: supplier not flagged, address not in `rfq_suppressions`, MX exists (`verification.email_deliverability.check_email_domain`). Failures become `skipped` with a reason, and never abort the batch.
7. At most one initial and one follow-up per (campaign, supplier, channel), enforced by a UNIQUE constraint.
8. Every email has a footer with buyer company, postal address, and "reply unsubscribe to stop". Opt-out replies (`unsubscribe`, `退订`) and hard bounces add the address to `rfq_suppressions` and are not parsed for quotes.
9. Strip CR/LF from subject and all header values.
10. Never auto-reply to a supplier. Follow-ups are drafts needing approval too.
11. Inbound email is untrusted: pass it to the LLM as delimited data, tell the model to ignore instructions inside it, and treat output purely as candidate fields.
12. Only store inbound mail that matches an RFQ. Discard everything else without persisting it, since the mailbox may be a personal one. Read with `BODY.PEEK[]` so messages are not marked read.
13. Passwords never appear in logs, API responses, or `/rfq/status`.

## Schema: migration v34

Follow the `MIGRATIONS` pattern, bump `SCHEMA_VERSION` to 34, add the same tables to `SCHEMA_SQL`, add to `table_counts`, and update `tests/test_capability_storage.py`'s version assertion.

- `rfq_campaigns`: id, title, product, product_zh, specification, quantity, unit, target_incoterm, destination, required_certifications (JSON), quote_deadline, status (`open`/`closed`), created_at
- `rfq_messages`: id, campaign_id, supplier_id, channel, kind (`initial`/`followup`), language, recipient, subject, body, action_url, reference (unique), status (`draft`/`approved`/`sending`/`sent`/`failed`/`skipped`/`bounced`/`replied`), status_reason, smtp_message_id, approved_at, sent_at, replied_at, created_at. UNIQUE(campaign_id, supplier_id, channel, kind)
- `rfq_replies`: id, message_id, campaign_id, supplier_id, channel, source (`imap`/`manual`), external_message_id (unique when not null), from_address, subject, body_text, attachments (JSON of filenames and read status), reply_type (`quote`/`bounce`/`opt_out`/`other`), parse_status (`pending`/`parsed`/`no_quote`/`failed`/`needs_manual_review`), received_at, created_at
- `rfq_quotes`: id, reply_id, campaign_id, supplier_id, unit_price, currency, quantity_basis, moq, lead_time, incoterm, payment_terms, valid_until, sample_terms, tooling_cost, evidence (JSON, field to exact quote), created_at. One row per price tier
- `rfq_suppressions`: email (primary key, lowercase), reason, created_at

The channel and status columns have no CHECK constraint, per the lesson in migrations v4 and v9.

## Code layout

- `storage/rfq_repository.py`: `RFQRepository(db_path=None)` using `connection_scope`
- `rfq/templates.py`: deterministic drafting, no LLM. Chinese-first bilingual for China, Taiwan and Hong Kong suppliers, using `product_zh` when provided, otherwise English for the product term. Never fabricate specs, company facts, or volumes
- `rfq/channels.py`: `SmtpSender`, `ImapInbox.fetch_recent(since_days, limit) -> list[bytes]`, link builders. Injectable, constructible without credentials
- `rfq/quote_parser.py`: `QuoteParser(llm=None)` using `llm.client.LLMClient.complete_json`
- `rfq/attachments.py`: text from PDF (`pypdf`, add to requirements.txt), XLSX (`openpyxl`, read-only), CSV, TXT. Cap at 10 MB per file and a total text limit. Unreadable (including scanned PDFs) gives `needs_manual_review`
- `rfq/rfq_service.py`: `RFQService` with injected repo, sender, inbox, parser, sleep function

Follow the dependency-injection pattern: tests need no network or credentials.

## Service behaviour

- `create_campaign(...)`, then `draft_messages(campaign_id, supplier_ids=None, query=None, limit=25, channels=None, all_channels=False)`. With `query`, use `SupplierRepository.search_suppliers`, which already excludes flagged suppliers; also skip flagged ids passed explicitly. Default is one channel per supplier, first available of: email, alibaba, made_in_china, whatsapp, wechat, contact_form. `all_channels=True` drafts all. Return counts, including `no_contact_route`.
- Email recipient is `primary_email`. Do not address secondary emails automatically.
- Reference token: `RFQ-{campaign_id}-{6 random uppercase alphanumerics}`, placed in the subject and body.
- `update_draft`, and `approve(campaign_id, message_ids=None)` for email drafts.
- `send_approved(campaign_id, limit=None)`: enforces all rules above, spaced by `RFQ_SEND_DELAY_SECONDS` (default 45) with jitter, process-wide semaphore, wall-clock `RFQ_SEND_JOB_MAX_SECONDS` budget returning `partial`. Sets `smtp_message_id`. Records procurement outcome `rfq_submitted`.
- `mark_sent(message_id)` for assisted channels, same outcome recorded.
- `poll_inbox(since_days=14, limit=200)`: dedupe on Message-ID. Match order: In-Reply-To/References against `smtp_message_id`, then reference token in subject or body, then exact sender address against a sent message's recipient. Bounces are detected via mailer-daemon/postmaster sender or `multipart/report`, and matched by token or original Message-ID. Set message status and `replied_at`, record outcome `quoted` when a quote is stored.
- `record_manual_reply(message_id, text)`: same parser for assisted channels.
- `draft_followups(campaign_id, after_days=3)`: for sent, unanswered, not bounced messages.
- `compare(campaign_id)`: quotes grouped by currency, sorted by unit price inside a group. Show incoterm, MOQ, lead time, and evidence side by side, plus who has not replied. Do NOT convert currencies, do NOT rank across currencies, and do NOT name a winner: incoterms differ, so the buyer decides. Do not write to any existing score field.

## Quote parser: grounded extraction

Strip quoted history from the email before parsing (`>` lines, "On ... wrote:", "-----Original Message", "发件人:"), otherwise our own RFQ's quantities get extracted. System prompt states the same discipline as `ADDRESS_EXTRACTION_SYSTEM_PROMPT`: only values explicitly stated, otherwise null.

Model returns `{"quotes": [...]}` with an exact-quote evidence string for each field. Validate in code:
- Evidence, whitespace-normalised, must be a substring of the source text. Otherwise drop that field.
- Numeric values (`unit_price`, `moq`, `tooling_cost`) must appear in their evidence, allowing thousands commas and decimal commas.
- `currency` must be stated in the evidence. Store as stated, normalising only RMB/CNY/元 to CNY and US$/USD to USD. Leave bare `$` and `¥` as stated.
- `lead_time` is stored as stated text, never converted.
- A quote with no valid `unit_price` is not stored; the reply becomes `no_quote` and stays readable.
- LLM unavailable: keep the reply with `parse_status=failed`, never lose it.

## Settings (`config/settings.py`, use the `os.getenv(key) or default` pattern)

`RFQ_SEND_ENABLED`, `RFQ_DAILY_SEND_LIMIT`, `RFQ_SEND_DELAY_SECONDS`, `RFQ_SEND_JOB_MAX_SECONDS`, `RFQ_SMTP_HOST`, `RFQ_SMTP_PORT` (587 STARTTLS, 465 SSL), `RFQ_IMAP_HOST`, `RFQ_IMAP_PORT` (993), `RFQ_IMAP_FOLDER` (INBOX), `RFQ_EMAIL_USER`, `RFQ_EMAIL_PASSWORD`, `RFQ_FROM_ADDRESS`, `RFQ_BUYER_COMPANY`, `RFQ_BUYER_NAME`, `RFQ_BUYER_ADDRESS`. Do not add these to `missing_api_keys()`.

## CLI: `main.py rfq ...`

`create`, `list`, `show`, `approve`, `send`, `followups`, `poll`, `reply`, `mark-sent`, `compare`, `check` (SMTP and IMAP login only, sends nothing), `test-email --to ADDRESS` (one test message to an address the user names). `send` prints the count, recipients, and today's remaining cap, then needs a confirmation prompt unless `--yes`.

## API (`api/app.py`, `api/models.py`, `api/jobs.py`)

All with `require_api_token`. Thin wrappers. Long work uses the existing `pipeline_jobs` + `BackgroundTasks` pattern.

`GET /rfq/status`, `POST /rfq/campaigns`, `GET /rfq/campaigns`, `GET /rfq/campaigns/{id}`, `PATCH /rfq/messages/{id}`, `POST /rfq/campaigns/{id}/approve`, `POST /rfq/campaigns/{id}/send` (202 job), `POST /rfq/poll` (202 job), `POST /rfq/messages/{id}/mark-sent`, `POST /rfq/messages/{id}/reply`, `POST /rfq/campaigns/{id}/followups`, `GET /rfq/campaigns/{id}/compare`.

## Frontend (`frontend/index.html`, vanilla JS, single file)

Add a "Quotes" nav button and view using existing `api()`, `esc()`, and styles. Escape all supplier and reply content with `esc()`, since it is untrusted. Shows: readiness banner from `/rfq/status`; campaign form; campaign list; per-campaign outreach table with editable drafts, approve, send; assisted rows with open link, copy message, mark sent, paste reply; a "check inbox" button; a comparison table with evidence. Do not touch `console.html` or `dashboard.html`.

## Docs

Add the env vars and endpoints to `DEPLOY.md`, and a short section to `README.md`, including the setup advice to use a dedicated mailbox rather than a personal one.

## Out of scope

Automating Alibaba, Made-in-China or WeChat accounts. A new Made-in-China scraper. Scheduled inbox polling. Currency conversion.

## Done means

- `tests/test_rfq_*.py` covers every safety rule and validator above, with fakes only
- `python -m pytest -q -n auto` passes in full
- Nothing committed; changes left in the working tree for review
- No real email sent, no paid API called
