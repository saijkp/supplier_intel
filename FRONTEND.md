# Supplier Intel — web frontend

Two self-contained single-file frontends live in `frontend/`, no build
step, no framework, no dependencies — Netlify serves either as-is and
they talk directly to your Railway API over HTTPS:

- **`index.html`** (deployed default, live at
  `https://grand-alfajores-a9d7ee.netlify.app/`) — the "Supplier Intel
  Console" dashboard UI: a sidebar-nav app (Dashboard, Suppliers,
  Pipeline Jobs, Buyer Profiles, Settings) backed by `GET
  /dashboard/summary` plus the same search/job endpoints below. See
  "Current default UI" further down. `frontend/dashboard.html` is an
  identical copy, also served at `/dashboard.html` on the same site —
  edit both together.
- **`legacy.html`** — the original four-tab evidence-rich app (Search,
  Buyer profiles, Pipeline, Compare). No longer the deployed default,
  kept for reference/rollback. The rest of this file below "Current
  default UI" documents *this* app.

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
API. The dot in the header turns green once `/health` answers.

Storage access is wrapped so that a browser or preview frame that
blocks local storage falls back to keeping the details in memory for
the session rather than breaking the page.

## Current default UI (`index.html` / `dashboard.html`)

Sidebar-nav dashboard console, five pages:

- **Dashboard** — `GET /dashboard/summary` in one call: entity mix,
  verified-high count vs. goal, daily/weekly verification and
  new-supplier trends, recent suppliers. Every stat card with a real
  backing filter is clickable, jumping to Suppliers or Pipeline Jobs
  pre-filtered (e.g. "Verified — High confidence" → `min_score=85`,
  "Avg daily verifications" → `verified_only=true`).
- **Suppliers** — search by product/country/min score/manufacturers-only/
  verified-only. Clicking a row opens a detail modal (`GET
  /suppliers/{id}`) with AI summary, strengths/risks, key contacts,
  matched capabilities with evidence, and certificates — the same
  underlying data the Legacy UI's evidence stamps below show, just in a
  different layout.
- **Pipeline Jobs** — start a search-driven pipeline run, poll status.
- **Buyer Profiles** — create/list saved buyer requirement bundles.
- **Settings** — the same API-address/access-token connection flow
  described in "First run" above.

## Legacy UI (`frontend/legacy.html`)

Everything below this point describes `legacy.html`'s four-tab app —
not the deployed default, kept for reference.

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
