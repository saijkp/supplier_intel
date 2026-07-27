# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A procurement research tool (~10,200 lines) originally scoped for trailer-component sourcing: given a product term (plus optional certifications, country, manufacturer-only filter), it scrapes multiple B2B sources, merges duplicate listings of the same real company, verifies who's an actual manufacturer vs. a trader, extracts capability/contact/certification evidence from each supplier's own website, scores the results, and returns them — via CLI or HTTP API — with the evidence attached, not just a verdict.

Read `README.md` first — it documents the 11-stage pipeline, and (important) an extensive, honest list of known limitations grouped by risk (data source coverage, verification accuracy, query capability, infrastructure, legal/data-protection). Don't re-derive those from the code; they're already written down. `DEPLOY.md` covers Railway deployment and the full HTTP endpoint table.

## Commands

```bash
pip install -r requirements.txt
python main.py init-db                     # create SQLite DB + schema
python main.py status                      # schema version + row counts
python main.py doctor                      # check config/API keys are present
python main.py doctor --live                # make a real minimal call to every configured integration (costs a small amount of real quota/money)
python main.py run "LED marker light"       # full scrape -> dedup -> verify -> score pipeline
python main.py search "wheel bearings" --require "iso 9001" --manufacturers-only
python main.py extract-capabilities --limit 30 --no-photos   # start small; costs OpenAI + real HTTP per supplier
python main.py find-websites --limit 20     # costs a paid SerpAPI call per domain-less supplier
python main.py sweep --limit 5              # run the full trailer search-term catalogue

# Tests
python -m pytest -q                         # full suite, runs against a temp SQLite file — safe anywhere
python -m pytest tests/test_pipeline.py -q  # single file
python -m pytest tests/test_pipeline.py::test_name -q   # single test

# API (local)
export API_ACCESS_TOKEN=dev-token
export ALLOWED_ORIGINS=http://localhost:3000
uvicorn api.app:app --reload
# docs at http://127.0.0.1:8000/docs
```

There is no lint/typecheck command configured in this repo (no ruff/mypy config found) — don't invent one; just run pytest.

Tests never touch the real database — `storage/database.py` and `storage/repository.py` are always pointed at a temporary SQLite file in test fixtures, so the full suite is safe to run against production-adjacent environments (e.g. a Railway shell) without risk to real data.

## Architecture

### Two-layer data model (`storage/database.py`, `storage/repository.py`)

- **Raw layer** (`raw_source_data`): every scrape payload stored verbatim as JSON, `processing_status` tracked (`pending`/`processed`/`failed`/`duplicate`), linked to the golden record it became.
- **Golden record layer** (`suppliers`): one row per real company, built by normalizing + deduplicating raw records. Wide table covering identity, location, contact, verification (USCC, manufacturer confidence), certifications, product intelligence, trade/shipment aggregates, commercial terms, platform presence, and composite scoring.
- Schema is versioned via `schema_migrations` + a `MIGRATIONS` dict in `storage/database.py` (currently v10). **Never edit `SCHEMA_SQL` for an existing column/table shape change** — add a new entry to `MIGRATIONS` instead, following the existing pattern (`columns` for simple `ALTER TABLE ADD COLUMN`, `statements` for anything needing a table rebuild, since SQLite can't `ALTER` a `CHECK` constraint in place — see migrations v4 and v9 for the rebuild pattern). `initialise_schema()` is idempotent and safe to call against a fresh or pre-existing DB.
- `SUPPLIER_WRITABLE_FIELDS` in `storage/repository.py` is the single source of truth for which normalizer output fields are allowed onto a `suppliers` row.

### Pipeline stages, one file per concern

The 11 README stages map to distinct modules, all orchestrated by `pipeline/orchestrator.py`'s `SupplierIntelligencePipeline`:

1. **Scrapers** (`scrapers/`) — one class per source (Alibaba, IndiaMART, HKTDC, ImportYeti/Volza trade data, Google, exhibition directories), all subclassing `BaseScraper` and returning a uniform `ScraperResult` (`source`, `source_id`, `raw_data`, `success`, `error`). A scraper must never raise for ordinary failures — it returns an error `ScraperResult` instead, so one bad source doesn't kill a multi-source run.
2. **Normalizers** (`normalizers/`) — one class per source, subclassing `BaseNormalizer`. Pure function: raw scraped dict in, `supplier_data` dict out (only fields `SUPPLIER_WRITABLE_FIELDS` recognises). Never touch the database. Must never raise on missing/malformed input — partial data beats losing the supplier.
3. **Deduplication** (`deduplication/matcher.py`) — `SupplierMatcher.find_match()` runs a three-level hierarchy: USCC exact match (1.00) → domain exact match (0.95) → fuzzy name+country match (rapidfuzz, boosted by phone/city, thresholds in `config/settings.py`: `DEDUP_AUTO_MERGE_THRESHOLD`/`DEDUP_HUMAN_REVIEW_THRESHOLD`/`DEDUP_REJECT_THRESHOLD`). Below-threshold-but-plausible matches queue into `dedup_candidates` for human review (`main.py review list/merge/reject`).
4. **Verification** (`verification/`) — independent modules, each owning one signal: `manufacturer_verifier.py` (Qichacha-based manufacturer-vs-trader verdict with plain-English reasons), `capability_extractor.py` (crawls a supplier's own site for in-house/subcontracted/asserted capability claims against a fixed vocabulary in `capability_vocabulary.py`), `facility_address_verifier.py` (country-routed: Google Places outside China, Amap within it), `linkedin_presence.py` (search-engine-only, never scrapes LinkedIn directly), `email_deliverability.py` (MX record check), `trust_signals.py` (phone/E-mark format sanity), `commercial_scoring.py`/`commercial_probability.py` (buyer-fit scoring), `scorer.py` (final composite score).
5. **Reports/export** (`reports/generator.py`) — Markdown reports and CSV export.
6. **API** (`api/`) — `app.py` is a thin FastAPI wrapper; all business logic stays in `pipeline/orchestrator.py` and `storage/repository.py`. `auth.py` is single shared-token auth that fails closed (503 if unconfigured, not silently open). `jobs.py` backs `POST /pipeline/jobs`, run in-process via FastAPI `BackgroundTasks` (not a real task queue — single-instance only, see README's infrastructure limitations).

Every stage in the pipeline follows the same idempotency discipline: a `*_at` timestamp column (`capability_extracted_at`, `website_search_attempted_at`, `facility_address_verified_at`, `linkedin_checked_at`) is set on **every attempt, matched or not** — so a supplier that was tried and found nothing isn't re-attempted (and re-billed against a paid API) on every subsequent run forever.

### Cost-awareness is a first-class design constraint

Several pipeline stages spend real money per supplier (SerpAPI, OpenAI, Google Places/Amap, Qichacha). This shows up throughout the codebase as:
- Expensive stages are opt-in flags on `main.py run` (`--find-websites`, `--extract-capabilities`, `--verify-facilities`, `--check-linkedin`), off by default.
- `--limit` flags default small and CLI help text explicitly recommends starting small (e.g. `extract-capabilities --limit 30 --no-photos` on a first run — factory photos go to a vision model and are "by far the most expensive part").
- `main.py doctor --live` exists specifically to verify a paid integration actually works with one minimal real call, separate from `doctor` (which only checks a key is *present*).
- Every scraper/verifier class is safe to construct without credentials — they only raise when actually invoked, so `SupplierIntelligencePipeline()` never fails to construct just because some optional API key is unset. Missing keys degrade gracefully (integration skipped), never silently break.

### Config (`config/settings.py`)

Single source of truth for paths, API keys, HS codes, product taxonomy, scoring weights, dedup thresholds, and rate limits. Two points worth knowing before touching it:
- `DB_PATH` deliberately uses `os.getenv(key) or default`, not the two-arg `os.getenv(key, default)` — a blank `SUPPLIER_INTEL_DB_PATH=` in `.env` must still fall back to the default, not resolve to an empty path (which silently tries to open the project directory itself as a SQLite file). Preserve this pattern for any new required-but-optional path/env var.
- `SCORING_WEIGHTS` and dedup thresholds are asserted at import time (weights must sum to 1.0; thresholds must be strictly increasing) — a typo fails loudly on import rather than silently skewing every score.

### Static import path

`pipeline/static_list_import.py` + `normalizers/automechanika_normalizer.py` handle bulk-importing a spreadsheet (e.g. an exhibitor list) through the *same* dedup/merge logic as a live scrape — a company already in the DB from Alibaba/HKTDC merges rather than duplicates. Use this as the template for adding another static/bulk source rather than writing bespoke import logic.

## Working in this codebase

- Every scraper, normalizer, and verifier is built to be dependency-injected (see `SupplierIntelligencePipeline.__init__`'s many `Optional[...] = None` constructor args) specifically so tests can substitute fakes with no network or credentials — follow this pattern for new integrations rather than reaching for module-level singletons or monkeypatching.
- When adding a new source: implement `BaseScraper.scrape()` (return `ScraperResult`s, never raise for ordinary failures) + a matching `BaseNormalizer.normalise()` (return partial data, never raise on malformed input), then wire both into `SupplierIntelligencePipeline`'s scraper/normalizer dicts in `pipeline/orchestrator.py`.
- When adding a schema change: add a new `MIGRATIONS` entry in `storage/database.py`, bump `SCHEMA_VERSION`, and add the same columns to `SCHEMA_SQL` so a fresh database also gets them directly.
- `verification/capability_vocabulary.py`'s vocabulary is a fixed, curated list (~36 terms) — an extracted capability outside it is stored as "unmapped," not discarded, and isn't searchable via `--require` until the vocabulary is extended.
