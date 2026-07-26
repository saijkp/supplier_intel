# Supplier Intelligence Platform

A procurement research tool: give it a product (and optionally
required certifications, a country, a manufacturer-only filter), and
it searches multiple B2B sources, merges duplicate listings of the
same real company, verifies who's an actual manufacturer versus a
trader, extracts capability/contact/certification evidence from each
supplier's own website, scores the results, and returns them —
CLI or HTTP API — with the evidence attached, not just a verdict.

~10,200 lines of application code, 674 automated tests, all passing
at time of writing. See `DEPLOY.md` for how to run it.

## What it actually does, stage by stage

1. **Discovery** — searches Alibaba, IndiaMART, HKTDC, ImportYeti,
   Volza (customs/trade data), Google, and exhibition directories
   (including Automechanika Frankfurt) for a product term.
2. **Deduplication** — merges the same real company found under
   slightly different names across sources: exact match on China's
   USCC registry number first, then shared domain, then fuzzy
   name+phone+city matching.
3. **Manufacturer verification** — Qichacha registry data (business
   scope, registered capital, tenure) distinguishes a real
   manufacturer from a trading company, with a plain-English reason
   for the verdict, not just a score.
4. **Website discovery** — for a listing with a company name but no
   usable website, searches the name and validates the result before
   trusting it (never blindly accepts the first search hit as the
   company's real site).
5. **Capability extraction** — visits each supplier's own website and
   reads it for manufacturing process/capability/certification
   language, distinguishing "we make this ourselves" from "we
   subcontract it" from "we merely sell it" — the single most
   important distinction for not confusing a factory with a reseller.
6. **Contact extraction** — pulls email/phone from the same page
   fetch, free (no LLM cost), including a fallback to a contact-form
   URL when no direct email exists.
7. **Facility verification** — confirms the claimed address resolves
   to a real place (Google Places outside China, Amap within it) and
   assesses whether photos on the supplier's own site plausibly show
   a real manufacturing facility.
8. **LinkedIn presence** — confirms a findable company page exists,
   via a search-engine query, never by scraping LinkedIn directly.
9. **Trust signals** — phone number format validity, email domain
   deliverability (a real, live-tested DNS check), E-mark number
   format sanity-checking.
10. **Scoring and search** — one query combines a product match,
    required certifications (all must be evidenced, not just one),
    a manufacturer-only filter, and a country filter, ranked by score,
    every result carrying the actual evidence quote it matched on.
11. **Export** — CSV, or a full HTTP API (`api/app.py`) for a frontend
    to build against, with async job handling for the slow stages.

## How to run it

- CLI: `python main.py search "wheel bearings" --require "iso 9001" --manufacturers-only`
- API: see `DEPLOY.md` for the Railway deployment walkthrough and the
  full endpoint table.
- Health check everything's actually working, not just configured:
  `python main.py doctor --live`

## Limitations — read this before trusting it in bulk

Grouped by what kind of risk each one is.

### Data source coverage

- **Several sources are actively blocked from cloud IPs.** `doctor
  --live` found HKTDC, ImportYeti, Volza, and multiple exhibition
  directories returning HTTP 403 from this environment — anti-bot
  blocking of datacenter traffic. Railway's IPs have the same problem.
  A residential/rotating proxy is the real fix, not yet built.
- **Alibaba/IndiaMART depend on third-party Apify actors** this
  codebase doesn't control — if those actors break or get shut down,
  scraping silently stops working until noticed.
- **No scraper has been proven against real result data.** Every check
  so far confirms *reachability* and *auth*, not that the parsing
  logic correctly extracts real listings from a real, current page —
  sites change their HTML without warning.

### Verification accuracy

- **Every paid-API integration (Qichacha, Google Places, Amap,
  OpenAI) is built against documented contracts, not exercised
  against a live key in this environment.** `doctor --live` is the
  tool to close that loop — run it before trusting any of these.
- **`ManufacturerVerifier`'s scoring weights have never been
  calibrated against known-good ground truth.** Run it against
  suppliers you've personally audited before trusting the verdict at
  volume — this advice has been given repeatedly through this
  project's build and is still outstanding.
- **E-mark checking is shape-only.** No public registry exists for a
  third party to verify a real E-mark number against (checked
  directly — UNECE's DETA database is restricted to contracting
  parties and manufacturers). A well-formed but fabricated number
  passes.
- **Factory photo verification trusts the photo is genuine.** Nothing
  cross-checks whether a "factory photo" is actually a stock image
  reused across many sites — reverse image search for this was
  scoped early on and never built.
- **Capability vocabulary is a fixed, ~36-term list**
  (`verification/capability_vocabulary.py`). A real capability outside
  that list is recorded as "unmapped," not lost, but isn't searchable
  via `--require` until someone reviews and extends the list.
- **LinkedIn presence is a weak signal, by design** — it proves a
  page exists, not that the company is trustworthy.
- **Email deliverability is domain-level only** (MX record check) —
  proves the domain can receive mail, not that the specific mailbox
  exists or is monitored.

### Query capability

- **Product matching is substring search, not semantic.** "13-inch
  wheel hub" won't match "wheel hub bearing assembly, 330mm" unless
  the stored text happens to overlap. No product-taxonomy or
  embedding-based matching exists.
- **No structured fields for annual capacity or lead time at all.**
  A query like "10,000 units/year, under 6 weeks lead time" can't be
  filtered on today — that data isn't captured anywhere in the
  schema.
- **No natural-language query parsing.** A buyer's full sentence
  needs translating into the structured `search()` call by hand (or
  by a frontend someone builds) — there's no LLM-based parser yet.
- **Country filter is exact match, not fuzzy** — "UK" must exactly
  match the stored string ("United Kingdom"), deliberately, to avoid
  "UK" silently matching "Ukraine."

### Infrastructure

- **Single-instance only.** Job execution runs in-process via
  FastAPI's `BackgroundTasks`, not a real task queue. A job started
  and the server restarting mid-run has no resume logic. This breaks
  silently across multiple Railway replicas — see `DEPLOY.md`'s own
  note on this.
- **One shared API token, no per-user accounts**, no audit log of who
  triggered what. Fine for one or two people; not fine the moment
  this has real external users.
- **No rate limiting.** A leaked token could trigger repeated
  pipeline runs and real OpenAI/SerpAPI/Google Places spend with
  nothing to stop it beyond the per-supplier gating logic already in
  the pipeline itself.
- **No error monitoring/alerting.** Failures surface in logs only —
  nothing pages anyone if something breaks quietly in production.
- **No CI pipeline set up yet** — regressions rely on manually running
  `pytest`, despite there now being 674 of them.
- **No frontend exists.** Netlify has nothing to serve until one is
  built — a separate, unstarted project.
- **Never load-tested at the actual 10,000-supplier target scale** —
  the LIKE-based text search has no index, and the fuzzy-matching
  passes are O(n^2). Likely fine at hundreds or low thousands of
  suppliers; unverified beyond that.

### Legal and data-protection

- **This system stores scraped personal data** — names, emails, phone
  numbers of individuals at supplier companies. UK GDPR applies to
  processing personal data regardless of where the individual is
  located if you're a UK-based controller. No retention policy,
  erasure-request handling, or documented lawful basis has been built
  in. Worth a real look before this goes beyond personal/internal use
  — genuinely not something to bolt on as an afterthought later.
- **Scraping ToS vary by site** and aren't tracked or enforced by this
  codebase beyond basic request pacing.
- **Deduplication is never 100% reliable** — fuzzy matching can
  occasionally merge two different real companies, or fail to merge
  the same one under very different names. Not stress-tested against
  a large, messy real-world dataset.

## How to make it better, roughly in priority order

1. **Set up CI** (GitHub Actions running `pytest` on every push).
   Cheap, mentioned before, still not done — the highest value-per-
   effort item on this whole list.
2. **Run a calibration pass**: pick suppliers you've personally
   audited, run the full pipeline against them, compare the verdicts
   to what you actually know. This is the single most-repeated,
   still-outstanding piece of advice across this entire build.
3. **A residential/rotating proxy** for the sources `doctor --live`
   found blocked, if those sources matter to your coverage.
4. **Lead-time and annual-capacity fields**, plus real product-
   taxonomy or embedding-based matching — the actual prerequisite for
   the natural-language-query workflow discussed earlier.
5. **A natural-language query parser** (LLM-based) translating a
   buyer's sentence into a structured search — the concrete next step
   toward "type a sentence, get a shortlist."
6. **Reverse image search** for factory photos, to catch stock-photo
   reuse — scoped, not built.
7. **A real task queue** (Celery + Redis, or Railway's own queue
   primitives) once there's enough traffic that single-instance,
   in-process background jobs stop being enough.
8. **Rate limiting** on the API, before this is exposed more broadly.
9. **A frontend** — Streamlit for a fast internal demo, or the full
   Netlify build for something presentable externally.
10. **A GDPR/data-retention review**, before any wider launch —
    genuinely worth doing earlier rather than later given the
    personal data this system collects and stores.
