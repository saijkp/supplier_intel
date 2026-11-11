# Supplier Intel — web frontend

Two self-contained single-file frontends live in `frontend/`, no build
step, no framework, no dependencies — Netlify serves either as-is and
they talk directly to your Railway API over HTTPS:

- **`index.html`** (deployed default, live at
  `https://grand-alfajores-a9d7ee.netlify.app/`) — the original
  four-tab evidence-rich app (Find Suppliers, Search, Bulk enrichment,
  Job History, Compare, Audit). The rest of this file below "Current
  default UI" documents *this* app.
- **`console.html`** — a newer "Supplier Intel Console" dashboard UI: a
  sidebar-nav app (Dashboard, Find suppliers, Pipeline Jobs, Buyer
  Profiles, Settings) backed by `GET /dashboard/summary` plus the same
  search/job endpoints below. Briefly the deployed default
  (2026-09-14 to 2026-09-16) before being reverted here in favour of
  the tested four-tab app above; preserved intact at `/console.html`
  in case that work is picked back up later, not currently linked from
  anywhere in the deployed `index.html`. `frontend/dashboard.html` is
  an identical copy of `console.html` (not of `index.html`), also
  served at `/dashboard.html` on the same site — edit those two
  together if the console work resumes.

## Deploy

**Drag and drop:** zip `index.html` + `netlify.toml` together, drop the
zip on app.netlify.com. Live in about ten seconds.

**From git:** commit both files (a `frontend/` folder in the existing
repo is fine), connect the repo in Netlify, set the base directory to
that folder. Build command stays empty; publish directory is `.`.

## The one step people miss

The API refuses browser requests from any origin not on its allow-list.
After the first deploy, add your Netlify URL to Railway:

```
ALLOWED_ORIGINS=https://your-site.netlify.app
```

Then redeploy the Railway service so it picks the value up. Without
this the app loads fine but every request fails — and the browser
reports it as a generic network error, which looks like the API being
down when it isn't. The app detects this case specifically and tells
you which origin needs adding.

For several origins (a preview URL and a production one, say), separate
them with commas and no spaces.

## First run

Open the site, click **Not connected**, and enter:

- **API address** — your Railway URL, no trailing slash
- **Access token** — the same string as `API_ACCESS_TOKEN` on Railway

Both are kept in this browser's local storage and sent only to your own
API. The dot turns green only after authenticated `/customers/status` succeeds. A 401 opens a single Settings banner; amber means the access token is missing or wrong, red means the API is unreachable.

Storage access is wrapped so that a browser or preview frame that
blocks local storage falls back to keeping the details in memory for
the session rather than breaking the page.

## Console UI (`console.html` / `dashboard.html`) — not currently deployed

Sidebar-nav dashboard console, five pages:

- **Dashboard** — `GET /dashboard/summary` in one call: entity mix,
  verified-high count vs. goal, daily/weekly verification and
  new-supplier trends, recent suppliers. Every stat card with a real
  backing filter is clickable, jumping to Find suppliers' browse view
  or Pipeline Jobs pre-filtered (e.g. "Verified — High confidence" →
  `min_score=85`, "Avg daily verifications" → `verified_only=true`).
- **Find suppliers** — single free-text box, classifies input the same
  way `deduplication.domain_utils.looks_like_url` does server-side (no
  spaces + contains a dot = URL):
  - Product/requirement or company name → always free: cleaned into a
    product term and run against `GET /suppliers/search` (existing
    database only, no paid discovery is ever auto-triggered by typing
    and hitting Go).
  - Website URL/bare domain → real cost, confirm-gated: shows an inline
    "Verify X? This visits the site directly" banner before calling
    `POST /companies/enrich`; on resolution opens the same supplier
    detail modal search results use.
  - "Attach a list" → `POST /batch/upload` (multipart), then switches to
    Pipeline Jobs to watch it run — real cost, one per row.
  - "Browse the database with filters instead" reveals the previous
    filter-form + table view (product/country/min score/manufacturers-
    only/verified-only; row click opens the same detail modal, with AI
    summary, strengths/risks, key contacts, matched capabilities with
    evidence, and certificates — the same underlying data the Legacy
    UI's evidence stamps below show, just in a different layout).
  - Not wired: the reference design's "Shortlists" sidebar item has no
    backing endpoint anywhere in this API, so it isn't in the sidebar —
    add a real save/shortlist capability first if that's wanted.
- **Pipeline Jobs** — start a search-driven pipeline run, poll status.
- **Buyer Profiles** — create/list saved buyer requirement bundles.
- **Settings** — the same API-address/access-token connection flow
  described in "First run" above.

## Current default UI (`frontend/index.html`)

Everything below this point describes `index.html`'s four-tab app —
the deployed default. (Note: some of the tab/feature names below may
have drifted from the actual current file — this section predates a
later within-app rewrite and hasn't been fully re-verified against it.)

**Search** — product, country, minimum score, and certifications that
must be evidenced. Requirements combine with AND: every selected term
has to be evidenced, or the supplier doesn't appear. Certifications are
offered as chips rather than a text box because the API rejects terms
outside its controlled vocabulary, and a rejected search is a worse
experience than a constrained one.

Each result shows where every claim came from and how strongly it's
held. **Show all evidence** pulls the complete set for a supplier —
the search results themselves only carry the evidence that matched
your filters, so an unfiltered search shows none until you ask.

**Buyer profiles** — a saved bundle of what a particular buyer needs.
Required fields (country, certifications, manufacturer-only) exclude
suppliers. Preferences (incoterm, payment terms, target market) only
affect ranking — a supplier with no DDP evidence still appears, just
lower down. **Find matches** scores every supplier against the profile
and shows the technical and commercial scores side by side, never
merged into one number.

**Pipeline** — starts a collection run and polls it while it works.
The optional stages each spend real API credit per supplier, so they're
off unless you tick them. CSV export downloads the current database.

**Audit → public "Verified" pass** — each supplier's detail view has a
Generate/Reinstate/Revoke/Regenerate control for an opt-in, public,
unauthenticated verification page (backed by `GET
/public/suppliers/{token}`, see `sharing/public_pass_service.py`).
Generating one issues a random token and shows its QR code and share
link right there; Revoke 404s the public page without deleting the
token, so Reinstate brings back the *same* link and QR code later
(useful for a physical printed card at a trade-show booth — it doesn't
need reprinting after a revoke/reinstate cycle). Regenerate is the only
action that issues a new token, invalidating any QR code already
printed. The public page itself is `frontend/verify.html`, a separate
noindex page (not part of the four-tab app) that a visitor reaches by
scanning the QR code or opening the share link directly — no login,
and it only ever shows the same curated, marketing-safe fields as the
API's public payload (certifications, UK registry status,
manufacturer-verification signals, capability findings) — never audit
verdicts, scores, or contact names.

## Reading the evidence stamps

Every claim carries a stamp showing its source tier and a confidence
bar:

| Stamp | Means |
|---|---|
| **Verified** | Customs/trade records or a business registry — third-party, strongest |
| **Located** | The supplier is physically based in the market in question |
| **Stated** | The supplier's own website says so, in its own words |
| **Claimed** | A directory listing checkbox — self-reported, weakest |
| **No data** | Nothing found. Not the same as "no" |

That last row is the important one. Nothing in this system treats
absent evidence as evidence of absence, and the interface doesn't
either — a factor with no data reads as unknown, never as a failure.

## Payment-terms estimates

Where the app shows a percentage for something like 60-day payment
terms, that is a prediction from company age, size, export activity and
manufacturer status — not a fact, and nobody has asked the supplier.
Every contributing signal is listed underneath with its weight, so the
number can be argued with rather than just trusted. The thresholds
behind it are uncalibrated starting points; recording real outcomes is
what will eventually make them meaningful.


## Daily workspace (November 2026 redesign)

The default home is Today: existing customer actions, unreplied inbox cards,
RFQ replies awaiting extraction, sent RFQs without a reply for 24 hours, and
running/failed jobs. Every action opens its existing workspace; refreshing
never sends, approves, polls mailboxes, extracts quotes, or starts paid work.
The action queue uses existing authenticated GET endpoints. Supplier identity
search adds an optional `identity` filter to `/suppliers/search` for name,
primary email and primary phone, preserving country and flagged filters.

Desktop navigation is a sidebar; phones have Today, Customers, RFQs, Research
and More at the bottom. Research preserves Find, Search, Bulk, Compare and
Audit as sub-tabs. Settings opens the existing connection dialog. Customer
stage counts filter the pipeline. Each enquiry opens a detail view containing
linked inbound mailbox cards, private WhatsApp screenshots, next action and
due date, profile, qualification, customer quotes and follow-up controls.
Email attachment download is unavailable in the current customer API.

Campaign progress counts distinct suppliers with extracted quotes against a
minimum of four per campaign product. Replies without extracted quotes do not
count. The existing item RFQ workspace remains linked for line-item sourcing.
The buyer still decides whether quotes are comparable and what to send.

The evergreen/ember palette uses semantic CSS variables. Theme cycles through
System, Light and Dark and saves the preference locally. Visible workspaces
refresh every 60 seconds, with elapsed update timestamps, skeletons, subtle
arrival pulses, confirmation toasts, keyboard focus and reduced-motion support.
Automatic refresh preserves customer detail edits and focused RFQ drafts.
Installed iPhone apps still require entering the token once inside the Home
Screen app. Shell cache version v2 replaces v1; private API data is not cached.
