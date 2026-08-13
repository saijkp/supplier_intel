"""
storage/database.py

SQLite connection management and schema creation for the Supplier
Intelligence Platform.

Design notes:
- Two-layer schema: raw_source_data (immutable scrape payloads) and
  suppliers (golden records) built via normalisation + deduplication.
- Schema is versioned through a simple `schema_migrations` table so
  future ALTER TABLE changes can be applied incrementally instead of
  re-running the full CREATE TABLE script against an existing DB.
- WAL mode + foreign_keys=ON are set on every connection.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config.settings import DB_PATH

logger = logging.getLogger(__name__)

# Bump this and add a migration function below whenever the schema changes.
SCHEMA_VERSION = 18


# ═══════════════════════════════════════════════════════════
# Schema DDL
# ═══════════════════════════════════════════════════════════

SCHEMA_SQL = """
-- ═══════════════════════════════════════
-- RAW LAYER: Preserve everything as-is
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS raw_source_data (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    source_id           TEXT,
    scraped_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_json            TEXT NOT NULL,
    processing_status   TEXT NOT NULL DEFAULT 'pending'
                        CHECK (processing_status IN
                            ('pending', 'processed', 'failed', 'duplicate')),
    golden_record_id    INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_source_data(source);
CREATE INDEX IF NOT EXISTS idx_raw_status ON raw_source_data(processing_status);
CREATE INDEX IF NOT EXISTS idx_raw_golden ON raw_source_data(golden_record_id);


-- ═══════════════════════════════════════
-- GOLDEN RECORD LAYER
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS suppliers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- IDENTITY
    canonical_name          TEXT NOT NULL,
    aliases                 TEXT,               -- JSON array
    domain                  TEXT UNIQUE,

    -- LOCATION
    country                 TEXT,
    province_state          TEXT,
    city                    TEXT,
    address                 TEXT,

    -- CONTACT
    primary_email           TEXT,
    secondary_emails        TEXT,               -- JSON array
    primary_phone           TEXT,
    whatsapp                TEXT,
    wechat_id               TEXT,
    linkedin_url            TEXT,
    contact_name            TEXT,
    contact_title           TEXT,

    -- VERIFICATION
    uscc                    TEXT UNIQUE,
    uscc_verified           BOOLEAN NOT NULL DEFAULT 0,
    uscc_verified_at        TIMESTAMP,
    company_reg_number      TEXT,
    is_manufacturer         BOOLEAN,            -- NULL = unknown
    manufacturer_confidence INTEGER NOT NULL DEFAULT 0
                            CHECK (manufacturer_confidence BETWEEN 0 AND 100),
    manufacturer_signals    TEXT,               -- JSON array: plain-English evidence/red-flags (v2)
    manufacturer_verified_at TIMESTAMP,         -- when ManufacturerVerifier last assessed this record (v2)
    capability_extracted_at TIMESTAMP,          -- when CapabilityExtractor last ran against this supplier's own website (v5) -- set on every attempt, regardless of whether any findings resulted, so a genuinely capability-less site is never re-attempted forever
    website_search_attempted_at TIMESTAMP,      -- when CompanyWebsiteFinder last tried to find this supplier's domain by name search (v6) -- set on every attempt, matched or not, so an unfindable company isn't re-searched (and re-billed via SerpAPI) on every run forever
    facility_address_verified BOOLEAN,          -- whether verification.facility_address_verifier resolved the claimed address to a real place (v7)
    facility_address_verification_source TEXT,  -- 'google_places' | 'amap' | 'unavailable' (v7)
    facility_address_verified_at TIMESTAMP,     -- set on every attempt, matched or not -- same never-attempted-vs-attempted-and-failed discipline as capability_extracted_at (v7)
    linkedin_checked_at TIMESTAMP,              -- when verification.linkedin_presence last checked for a company page; linkedin_url (already existed since Phase 1) is set only if one was found (v7)
    contact_form_url TEXT,                      -- set when a supplier's own site has a contact form but no email/phone was ever found -- see verification.website_contact_extractor.best_contact_method (v7)
    registered_capital_rmb  REAL,               -- from Qichacha; red-flag signal, not a revenue figure (v2)
    business_scope          TEXT,               -- raw registered business-scope text from Qichacha (v2)
    factory_photo_urls      TEXT,               -- JSON array: captured photo URLs awaiting visual assessment (v2)
    factory_photo_verdict   TEXT,               -- 'plausible_factory' | 'implausible' | 'uncertain' (v2)
    factory_photo_assessed_at TIMESTAMP,        -- (v2)
    year_established        INTEGER,
    employee_count          TEXT,
    factory_size_sqm        INTEGER,
    annual_revenue_usd      TEXT,

    -- CERTIFICATIONS
    iso_9001                BOOLEAN NOT NULL DEFAULT 0,
    iso_9001_expiry         DATE,
    iso_ts_16949            BOOLEAN NOT NULL DEFAULT 0,
    e_mark_certified        BOOLEAN NOT NULL DEFAULT 0,
    e_mark_numbers          TEXT,               -- JSON array
    ce_certified            BOOLEAN NOT NULL DEFAULT 0,
    ukca_certified          BOOLEAN NOT NULL DEFAULT 0,
    iatf_16949              BOOLEAN NOT NULL DEFAULT 0,
    other_certifications    TEXT,               -- JSON array

    -- PRODUCT INTELLIGENCE
    primary_categories      TEXT,               -- JSON array
    product_keywords        TEXT,               -- JSON array
    trailer_components      TEXT,               -- JSON array
    moq_notes               TEXT,

    -- TRADE INTELLIGENCE
    exports_to_uk           BOOLEAN NOT NULL DEFAULT 0,
    exports_to_eu           BOOLEAN NOT NULL DEFAULT 0,
    exports_to_us           BOOLEAN NOT NULL DEFAULT 0,
    active_export_countries TEXT,               -- JSON array
    confirmed_shipments_uk  INTEGER NOT NULL DEFAULT 0,
    confirmed_shipments_eu  INTEGER NOT NULL DEFAULT 0,
    confirmed_shipments_us  INTEGER NOT NULL DEFAULT 0,
    last_shipment_date      DATE,
    annual_export_volume    TEXT,
    known_buyers            TEXT,               -- JSON array
    sinosure_coverage       BOOLEAN,

    -- COMMERCIAL TERMS
    payment_terms_offered   TEXT,               -- JSON array
    incoterms_supported     TEXT,               -- JSON array
    currencies_accepted     TEXT,               -- JSON array
    can_do_ddp_uk           BOOLEAN NOT NULL DEFAULT 0,

    -- PLATFORM PRESENCE
    alibaba_url             TEXT,
    alibaba_gold_supplier   BOOLEAN NOT NULL DEFAULT 0,
    alibaba_years           INTEGER,
    alibaba_trade_assurance BOOLEAN NOT NULL DEFAULT 0,
    alibaba_rating          REAL,
    indiamart_url           TEXT,
    hktdc_url               TEXT,
    made_in_china_url       TEXT,

    -- SCORING
    -- product_fit_score/provenance_score/evidence_coverage (v16) -- see
    -- verification/scorer.py's rewrite. platform_score is retained
    -- (still written by SupplierScorer._platform_score) but no longer a
    -- weighted composite dimension -- it now only feeds a small capped
    -- bonus, same demotion as uscc_verified (v16).
    product_fit_score        INTEGER NOT NULL DEFAULT 0
                             CHECK (product_fit_score BETWEEN 0 AND 100),
    provenance_score         INTEGER NOT NULL DEFAULT 0
                             CHECK (provenance_score BETWEEN 0 AND 100),
    verification_score      INTEGER NOT NULL DEFAULT 0
                             CHECK (verification_score BETWEEN 0 AND 100),
    self_asserted_score     INTEGER,             -- 0-100, no DB CHECK (v17) -- see MIGRATIONS[17]'s own comment for why (same reason as ai_confidence_score)
    export_score             INTEGER NOT NULL DEFAULT 0
                             CHECK (export_score BETWEEN 0 AND 100),
    platform_score           INTEGER NOT NULL DEFAULT 0
                             CHECK (platform_score BETWEEN 0 AND 100),
    contact_score             INTEGER NOT NULL DEFAULT 0
                             CHECK (contact_score BETWEEN 0 AND 100),
    composite_score           INTEGER NOT NULL DEFAULT 0
                             CHECK (composite_score BETWEEN 0 AND 100),
    evidence_coverage        INTEGER NOT NULL DEFAULT 0
                             CHECK (evidence_coverage BETWEEN 0 AND 100),
    recommendation            TEXT NOT NULL DEFAULT 'unverified'
                             CHECK (recommendation IN
                                 ('recommended', 'review', 'unverified', 'unscored', 'avoid')),

    -- METADATA
    source_count             INTEGER NOT NULL DEFAULT 1,
    first_seen                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_verified               TIMESTAMP,     -- set by verification_ai.VerificationService.verify() (v11) -- declared since Phase 1 but never written until now
    notes                        TEXT,
    flagged                      BOOLEAN NOT NULL DEFAULT 0,
    flag_reason                  TEXT,

    -- AI VERIFICATION (v11) -- see verification_ai/, deliberately separate from
    -- the rule-based composite_score/manufacturer_confidence above (never blended,
    -- same precedent as commercial_compatibility_score elsewhere in this codebase)
    ai_confidence_score          INTEGER,       -- 0-100, validated in storage/repository.py's write path, not a DB CHECK -- SQLite's ALTER TABLE ADD COLUMN can't add one consistently with a fresh-DB CREATE TABLE, so this stays consistent between both paths deliberately (see MIGRATIONS[11])
    ai_confidence_assessed_at    TIMESTAMP,
    ai_summary                   TEXT,
    ai_strengths                 TEXT,          -- JSON array
    ai_risks                     TEXT,          -- JSON array
    ai_suitable_customer_types   TEXT,          -- JSON array
    ai_verification_model        TEXT,          -- e.g. "gpt-4o-mini" -- traceability

    -- DISCOVERY / COLLECTION PROVENANCE (v11)
    discovery_source              TEXT,         -- 'discovery_service' | NULL (legacy/unset)
    collection_last_run_at        TIMESTAMP,
    collection_status             TEXT,         -- 'never_run' | 'success' | 'partial' | 'failed'

    -- SOURCING AGENT procurement dossier (v12) -- see sourcing/dossier_generator.py.
    -- Deliberately separate from ai_summary/ai_strengths/ai_risks above (different
    -- question: "does this supplier satisfy THIS chat brief's checklist," not a
    -- general-purpose assessment) -- never blended, same precedent as
    -- ai_confidence_score vs composite_score.
    sourcing_oem_odm_notes         TEXT,
    sourcing_factory_notes         TEXT,
    sourcing_engineering_notes     TEXT,
    sourcing_export_notes          TEXT,
    sourcing_volume_suitability    TEXT,
    sourcing_payment_terms_notes   TEXT,
    sourcing_verification_status   TEXT,         -- 'verified' | 'partially verified' | 'unverified' -- no CHECK, same "avoid a fixed enum that breaks the moment a new value is needed" precedent as procurement_outcomes.outcome

    -- Apollo named-contact discovery (v13) -- see
    -- verification/apollo_contact_finder.py and
    -- verification/contact_finder_service.py. A separate, opt-in
    -- enrichment stage (like Collect/Verify), never run automatically
    -- during a Sourcing Agent run.
    key_contacts                   TEXT,         -- JSON array of {name, title, email, phone, linkedin_url, role_category}
    contacts_found_at              TIMESTAMP,    -- set on every attempt, matched or not -- same never-attempted-vs-attempted-and-found-nothing discipline as capability_extracted_at

    -- Procurement Decision Engine foundation (v14) -- see
    -- verification_ai/confidence_scorer.py (ConfidenceScoreResult) and
    -- verification_ai/procurement_recommendation.py.
    ai_confidence_breakdown        TEXT,         -- JSON array of {name, weight, verdict, contribution}
    procurement_recommendation     TEXT,         -- no DB CHECK -- see MIGRATIONS[14]'s own comment for why (same reason as ai_confidence_score)
    procurement_recommendation_reason TEXT,

    -- Procurement Decision Engine Phase 3 (v15) -- see
    -- collection/site_collector.py (certificate detection+download) and
    -- verification/factory_facts_extractor.py /
    -- verification/factory_facts_service.py.
    certificate_document_urls      TEXT,         -- JSON array of {url, matched_keyword, filename, artifact_path}
    production_lines_notes         TEXT,
    machinery_notes                TEXT,
    factory_ownership               TEXT,        -- no DB CHECK -- see MIGRATIONS[15]'s own comment for why
    factory_facts_extracted_at     TIMESTAMP     -- set on every attempt, matched or not -- same discipline as capability_extracted_at/contacts_found_at
);

CREATE INDEX IF NOT EXISTS idx_sup_domain ON suppliers(domain);
CREATE INDEX IF NOT EXISTS idx_sup_uscc ON suppliers(uscc);
CREATE INDEX IF NOT EXISTS idx_sup_country ON suppliers(country);
CREATE INDEX IF NOT EXISTS idx_sup_score ON suppliers(composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_sup_recommendation ON suppliers(recommendation);
CREATE INDEX IF NOT EXISTS idx_sup_e_mark ON suppliers(e_mark_certified);
CREATE INDEX IF NOT EXISTS idx_sup_manufacturer ON suppliers(is_manufacturer);
CREATE INDEX IF NOT EXISTS idx_sup_canonical_name ON suppliers(canonical_name);


-- ═══════════════════════════════════════
-- SHIPMENT RECORDS (from Panjiva/ImportYeti)
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS shipment_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    source              TEXT NOT NULL,  -- see config.settings.VALID_SHIPMENT_SOURCES for the reference list (not DB-enforced — new trade sources are added over time; see migration v4)
    shipper_name        TEXT,
    consignee_name      TEXT,
    consignee_country   TEXT,
    shipment_date       DATE,
    hs_code             TEXT,
    product_desc        TEXT,
    quantity            TEXT,
    weight_kg           REAL,
    value_usd           REAL,
    origin_port         TEXT,
    destination_port    TEXT,
    raw_record          TEXT
);

CREATE INDEX IF NOT EXISTS idx_ship_supplier ON shipment_records(supplier_id);
CREATE INDEX IF NOT EXISTS idx_ship_date ON shipment_records(shipment_date);
CREATE INDEX IF NOT EXISTS idx_ship_hs ON shipment_records(hs_code);


-- ═══════════════════════════════════════
-- DEDUPLICATION CANDIDATES (human review queue)
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS dedup_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id_a   INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    supplier_id_b   INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    match_score     REAL NOT NULL CHECK (match_score BETWEEN 0.0 AND 1.0),
    match_signals   TEXT,               -- JSON
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'merged', 'rejected')),
    reviewed_at     TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dedup_status ON dedup_candidates(status);
CREATE INDEX IF NOT EXISTS idx_dedup_a ON dedup_candidates(supplier_id_a);
CREATE INDEX IF NOT EXISTS idx_dedup_b ON dedup_candidates(supplier_id_b);


-- ═══════════════════════════════════════
-- MANUFACTURING CAPABILITY FINDINGS (v5)
-- One row per (supplier, canonical_term, relationship) observation
-- extracted from the supplier's own website by
-- verification.capability_extractor.CapabilityExtractor. Kept as its
-- own table, not a JSON column on suppliers, because a supplier can
-- have several capabilities and each needs its own confidence/
-- evidence/source_url — exactly the shape shipment_records already
-- uses for the analogous one-to-many trade-data relationship.
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS supplier_capabilities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    reported_term       TEXT NOT NULL,   -- verbatim model output before vocabulary mapping
    canonical_term      TEXT,            -- NULL when the vocabulary doesn't recognise reported_term yet
    category            TEXT,            -- 'process' | 'capability' | 'standard' — NULL alongside canonical_term
    relationship        TEXT NOT NULL CHECK (relationship IN ('in_house', 'subcontracted', 'asserted')),
                                          -- 'asserted' (v9): for claims with no in-house-vs-subcontracted
                                          -- distinction at all (e.g. "serves the UK market") -- see
                                          -- capability_vocabulary's commercial-intelligence extension
                                          -- 'sold_only' observations are never stored — see capability_extractor
    confidence          REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    evidence            TEXT NOT NULL,   -- the supporting quotation from the page
    source_url          TEXT,
    assessed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (supplier_id, reported_term, relationship)
    -- Deliberately not canonical_term: it can be NULL for an unmapped
    -- term, and SQLite (standard SQL) never treats two NULLs as equal
    -- for a UNIQUE constraint, which would silently defeat idempotency
    -- for exactly the unmapped-term case this table most needs to
    -- de-duplicate. reported_term is always NOT NULL and
    -- canonical_term is a pure, deterministic function of it anyway
    -- (see capability_vocabulary.map_to_canonical), so it adds nothing
    -- to the uniqueness key.
);

CREATE INDEX IF NOT EXISTS idx_cap_supplier ON supplier_capabilities(supplier_id);
CREATE INDEX IF NOT EXISTS idx_cap_canonical ON supplier_capabilities(canonical_term);


-- ═══════════════════════════════════════
-- ASYNC PIPELINE JOBS (v8)
-- Backs the FastAPI layer's job-trigger endpoint: `pipeline.run()` and
-- its heavier stages (capability extraction, facility verification,
-- LinkedIn checks) can take minutes, far too long for a synchronous
-- HTTP request, so the API returns a job id immediately and the
-- frontend polls this table's status via GET /pipeline/jobs/{id}.
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id              TEXT PRIMARY KEY,     -- UUID, generated by the API layer
    query           TEXT NOT NULL,
    options         TEXT,                 -- JSON: which run() flags were requested
    status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    stats           TEXT,                 -- JSON: run()'s own stats dict, once complete
    error           TEXT,
    progress        TEXT,                 -- JSON (v12): live incremental status for a long-running job
                                           -- (currently only written by sourcing/ -- see
                                           -- SupplierRepository.update_pipeline_job_progress), polled
                                           -- the same way `stats` already is, just before completion
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON pipeline_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON pipeline_jobs(created_at);


-- ═══════════════════════════════════════
-- COMMERCIAL INTELLIGENCE: BUYER PROFILES + PROCUREMENT OUTCOMES (v10)
-- A Buyer Profile is a named, reusable bundle of the same filters
-- search_suppliers_full already accepts, plus commercial preferences
-- (incoterm, payment terms, target market) scored rather than
-- filtered on -- see pipeline.buyer_profile_search's own docstring
-- for exactly how required vs preferred fields are treated
-- differently. required_capabilities is stored as JSON, matching
-- every other JSON-array field on suppliers (product_keywords, etc).
--
-- procurement_outcomes is schema + repository methods only, per the
-- commercial-intelligence spec's own instruction -- no UI, and
-- currently no automated scoring calibration reads from it either
-- (see commercial_probability.py's own note on this being the future
-- calibration source once real outcomes accumulate). `outcome` is
-- deliberately NOT constrained by a CHECK -- the same lesson v4 and
-- v9 already both taught this codebase: a fixed list breaks the
-- moment a new outcome type is needed, and rebuilding a table to fix
-- a CHECK constraint is real, avoidable migration work.
-- ═══════════════════════════════════════

CREATE TABLE IF NOT EXISTS buyer_profiles (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    name                            TEXT NOT NULL UNIQUE,
    destination_country             TEXT,
    required_capabilities           TEXT,     -- JSON array, e.g. ["iso 9001"]
    preferred_incoterm              TEXT,     -- e.g. "ddp shipping" -- scored, not filtered
    preferred_payment_terms_days    INTEGER,
    min_company_size                TEXT,     -- free text (e.g. "medium+") -- deliberately not a numeric
                                               -- tier; see commercial_scoring.assess_company_scale's own
                                               -- note on why fabricated precision is worse than honest text
    target_market                   TEXT,     -- e.g. "oem"
    min_export_experience_years     INTEGER,
    manufacturers_only              BOOLEAN NOT NULL DEFAULT 1,
    created_at                      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS procurement_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    buyer_profile_id    INTEGER REFERENCES buyer_profiles(id) ON DELETE SET NULL,
    outcome             TEXT NOT NULL,   -- e.g. 'nda_signed', 'rfq_submitted', 'quoted',
                                          -- 'accepted_ddp', 'accepted_60_day_payment',
                                          -- 'tooling_order_won', 'quality_approved', 'supplier_rejected'
                                          -- -- deliberately unconstrained, see section comment above
    notes               TEXT,
    recorded_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outcomes_supplier ON procurement_outcomes(supplier_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_profile ON procurement_outcomes(buyer_profile_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON procurement_outcomes(outcome);


-- ═══════════════════════════════════════
-- SEARCH QUERIES LOG
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS search_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT NOT NULL,
    category        TEXT,
    sources_used    TEXT,               -- JSON array
    results_count   INTEGER NOT NULL DEFAULT 0,
    searched_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_feedback   TEXT
);

CREATE INDEX IF NOT EXISTS idx_search_query ON search_log(query);
CREATE INDEX IF NOT EXISTS idx_search_date ON search_log(searched_at);


-- ═══════════════════════════════════════
-- SOURCE/QUERY SCRAPE TRACKING (v3)
-- Tracks the last time a given (source, query) pair was actually
-- scraped, so the pipeline can skip re-scraping combinations it already
-- has recent data for — addresses Phase 1 Gap 4 (incremental updates).
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS source_query_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    query           TEXT NOT NULL,
    run_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    results_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sqr_source_query ON source_query_runs(source, query);
CREATE INDEX IF NOT EXISTS idx_sqr_run_at ON source_query_runs(run_at);


-- ═══════════════════════════════════════
-- AI DISCOVERY/COLLECTION/VERIFICATION PLATFORM (v11)
-- Append-only history/evidence tables backing verification_ai/,
-- collection/, and discovery/ -- see storage/database.py's MIGRATIONS[11]
-- description and CLAUDE.md for the overall architecture. raw_source_data
-- (above) is reused as-is for discovery evidence (source='discovery');
-- these four tables are the genuinely new schema surface.
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS verification_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    verification_type   TEXT NOT NULL,   -- 'ai_cross_check' | 'manufacturer_assessment' | 'facility_address' | 'linkedin' | 'uscc' -- deliberately unconstrained, same reasoning as v10's procurement_outcomes.outcome
    confidence_score    INTEGER,
    verdict              TEXT,
    summary               TEXT,
    evidence_json         TEXT,           -- JSON: sub-check results/citations for this run
    model_used             TEXT,
    run_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vh_supplier ON verification_history(supplier_id);
CREATE INDEX IF NOT EXISTS idx_vh_type ON verification_history(verification_type);


CREATE TABLE IF NOT EXISTS supplier_change_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    field_name      TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    changed_by      TEXT NOT NULL,   -- 'collection_service' | 'verification_service' | 'discovery_service' | 'merge' | 'manual'
    change_reason   TEXT,
    changed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scl_supplier ON supplier_change_log(supplier_id);
CREATE INDEX IF NOT EXISTS idx_scl_field ON supplier_change_log(field_name);


CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    pages_visited   INTEGER NOT NULL DEFAULT 0,
    artifacts_dir   TEXT,             -- path relative to config.settings.COLLECTION_ARTIFACTS_DIR
    proxy_provider  TEXT,
    error_message   TEXT,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cr_supplier ON collection_runs(supplier_id);


CREATE TABLE IF NOT EXISTS discovery_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    product_query           TEXT NOT NULL,
    category                TEXT,
    country                 TEXT,
    candidates_found        INTEGER NOT NULL DEFAULT 0,
    candidates_validated    INTEGER NOT NULL DEFAULT 0,
    candidates_rejected     INTEGER NOT NULL DEFAULT 0,
    candidates_duplicate    INTEGER NOT NULL DEFAULT 0,  -- matched an existing supplier via SupplierMatcher
    run_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dr_query ON discovery_runs(product_query);


-- Sourcing Agent (v12): one row per chat brief, scoping its qualified
-- results + CSV download to exactly this request rather than the whole
-- suppliers table. See sourcing/sourcing_agent.py.
CREATE TABLE IF NOT EXISTS sourcing_runs (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_text                  TEXT NOT NULL,
    structured_brief_json       TEXT,
    target_count                INTEGER NOT NULL,
    examined_count              INTEGER NOT NULL DEFAULT 0,
    qualified_supplier_ids_json TEXT,
    status                      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
    error_message                TEXT,
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at                TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sruns_status ON sourcing_runs(status);


-- CSV batch upload (v18): one row per uploaded CSV row, tracked
-- separately from suppliers since a row may not resolve to one yet
-- (needs_url/needs_name) and the original spreadsheet columns need to
-- survive untouched through to export regardless of enrichment outcome.
-- batch_job_id is a pipeline_jobs.id -- batch upload reuses the same
-- job-tracking table/polling mechanism every other async stage does,
-- rather than introducing a second job concept.
CREATE TABLE IF NOT EXISTS batch_upload_rows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_job_id        TEXT NOT NULL REFERENCES pipeline_jobs(id) ON DELETE CASCADE,
    row_index           INTEGER NOT NULL,
    original_columns    TEXT NOT NULL,      -- JSON object: every column from the uploaded row, verbatim, for export pass-through
    company_name        TEXT,               -- resolved name -- either straight from the CSV or a domain-derived placeholder (see name_source)
    name_source          TEXT NOT NULL DEFAULT 'csv'
                          CHECK (name_source IN ('csv', 'inferred_from_domain')),
    website               TEXT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'needs_url', 'needs_name', 'processing', 'success', 'failed')),
    supplier_id            INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    error_message          TEXT,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_batch_rows_job ON batch_upload_rows(batch_job_id);
CREATE INDEX IF NOT EXISTS idx_batch_rows_supplier ON batch_upload_rows(supplier_id);


-- Field-level provenance (v18): source_url/raw_snippet/extraction_method
-- for an individual extracted value, plus the two inputs confidence is
-- derived from -- source_tier (was this the company's own domain, or
-- somewhere else) and claim_type (a verifiable fact, or a self-assessed
-- claim -- certifications/capacity/headcount/manufacturer-vs-trader are
-- marketing copy even from the company's own site). Deliberately stores
-- the two inputs, not a single pre-computed confidence value, since the
-- calibration report (step 3) needs to group by source_tier
-- independently of claim_type, not just read a final label.
-- confidence = high only when source_tier='own_domain' AND
-- claim_type='verifiable_fact'; everything else is low by default.
-- Only written for canonical_name (the domain-derived-placeholder
-- replacement case) as of v18/batch/ -- extended to every other
-- extracted field as part of the provenance feature proper.
CREATE TABLE IF NOT EXISTS field_provenance (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    field_name          TEXT NOT NULL,
    value                 TEXT,
    source_url             TEXT,
    raw_snippet            TEXT,
    extraction_method      TEXT NOT NULL,   -- e.g. 'llm_grounded_extraction', 'regex', 'platform_listing'
    source_tier             TEXT NOT NULL CHECK (source_tier IN ('own_domain', 'other')),
    claim_type               TEXT NOT NULL CHECK (claim_type IN ('verifiable_fact', 'self_assessed')),
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_field_provenance_supplier ON field_provenance(supplier_id);
CREATE INDEX IF NOT EXISTS idx_field_provenance_field ON field_provenance(supplier_id, field_name);


-- ═══════════════════════════════════════
-- SCHEMA VERSIONING
-- ═══════════════════════════════════════
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER PRIMARY KEY,
    applied_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description     TEXT
);
"""


# ═══════════════════════════════════════════════════════════
# Incremental migrations (for databases created under an older
# SCHEMA_VERSION). Fresh databases already get every column/table via
# SCHEMA_SQL above — these exist purely to bring an *existing* database
# up to date. Two kinds of change are supported per migration:
#   - "columns": [(table, column, sql_type), ...] applied via
#     _add_column_if_missing (safe to re-run; skips if already present)
#   - "statements": [sql, ...] raw idempotent SQL (CREATE TABLE/INDEX
#     IF NOT EXISTS) for anything beyond a simple column addition, e.g.
#     a brand new table
# ═══════════════════════════════════════════════════════════

MIGRATIONS: dict[int, dict] = {
    2: {
        "description": (
            "Manufacturer verification: registered_capital_rmb, business_scope, "
            "manufacturer_signals, manufacturer_verified_at, factory_photo_urls, "
            "factory_photo_verdict, factory_photo_assessed_at on suppliers"
        ),
        "columns": [
            ("suppliers", "manufacturer_signals", "TEXT"),
            ("suppliers", "manufacturer_verified_at", "TIMESTAMP"),
            ("suppliers", "registered_capital_rmb", "REAL"),
            ("suppliers", "business_scope", "TEXT"),
            ("suppliers", "factory_photo_urls", "TEXT"),
            ("suppliers", "factory_photo_verdict", "TEXT"),
            ("suppliers", "factory_photo_assessed_at", "TIMESTAMP"),
        ],
    },
    3: {
        "description": (
            "Incremental scraping: source_query_runs table, tracking the last "
            "time each (source, query) pair was scraped"
        ),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS source_query_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT NOT NULL,
                query           TEXT NOT NULL,
                run_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                results_count   INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sqr_source_query ON source_query_runs(source, query)",
            "CREATE INDEX IF NOT EXISTS idx_sqr_run_at ON source_query_runs(run_at)",
        ],
    },
    4: {
        "description": (
            "Drop shipment_records.source CHECK constraint (was hardcoded to "
            "'panjiva'/'importyeti' and broke the first new trade source added, "
            "'volza' — SQLite can't ALTER a CHECK constraint in place, so this "
            "rebuilds the table without it, matching raw_source_data.source's "
            "already-unconstrained design)"
        ),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS shipment_records_v4 (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id         INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
                source              TEXT NOT NULL,
                shipper_name        TEXT,
                consignee_name      TEXT,
                consignee_country   TEXT,
                shipment_date       DATE,
                hs_code             TEXT,
                product_desc        TEXT,
                quantity            TEXT,
                weight_kg           REAL,
                value_usd           REAL,
                origin_port         TEXT,
                destination_port    TEXT,
                raw_record          TEXT
            )
            """,
            """
            INSERT INTO shipment_records_v4 (
                id, supplier_id, source, shipper_name, consignee_name,
                consignee_country, shipment_date, hs_code, product_desc,
                quantity, weight_kg, value_usd, origin_port, destination_port, raw_record
            )
            SELECT
                id, supplier_id, source, shipper_name, consignee_name,
                consignee_country, shipment_date, hs_code, product_desc,
                quantity, weight_kg, value_usd, origin_port, destination_port, raw_record
            FROM shipment_records
            """,
            "DROP TABLE shipment_records",
            "ALTER TABLE shipment_records_v4 RENAME TO shipment_records",
            "CREATE INDEX IF NOT EXISTS idx_ship_supplier ON shipment_records(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_ship_date ON shipment_records(shipment_date)",
            "CREATE INDEX IF NOT EXISTS idx_ship_hs ON shipment_records(hs_code)",
        ],
    },
    5: {
        "description": (
            "Manufacturing capability findings: supplier_capabilities table and "
            "suppliers.capability_extracted_at, populated by "
            "verification.capability_extractor from a supplier's own website"
        ),
        "columns": [
            ("suppliers", "capability_extracted_at", "TIMESTAMP"),
        ],
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS supplier_capabilities (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                reported_term       TEXT NOT NULL,
                canonical_term      TEXT,
                category            TEXT,
                relationship        TEXT NOT NULL CHECK (relationship IN ('in_house', 'subcontracted')),
                confidence          REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
                evidence            TEXT NOT NULL,
                source_url          TEXT,
                assessed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (supplier_id, reported_term, relationship)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_cap_supplier ON supplier_capabilities(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_cap_canonical ON supplier_capabilities(canonical_term)",
        ],
    },
    6: {
        "description": (
            "suppliers.website_search_attempted_at -- tracks whether "
            "scrapers.company_website_finder.CompanyWebsiteFinder has tried to find a "
            "domain for suppliers who never had one, so an unfindable company isn't "
            "re-searched (and re-billed via SerpAPI) on every run forever"
        ),
        "columns": [
            ("suppliers", "website_search_attempted_at", "TIMESTAMP"),
        ],
        "statements": [],
    },
    7: {
        "description": (
            "Facility address verification (verification.facility_address_verifier, "
            "country-routed: Google Places outside China, Amap within it) and LinkedIn "
            "presence checking (verification.linkedin_presence) tracking columns"
        ),
        "columns": [
            ("suppliers", "facility_address_verified", "BOOLEAN"),
            ("suppliers", "facility_address_verification_source", "TEXT"),
            ("suppliers", "facility_address_verified_at", "TIMESTAMP"),
            ("suppliers", "linkedin_checked_at", "TIMESTAMP"),
            ("suppliers", "contact_form_url", "TEXT"),
        ],
        "statements": [],
    },
    8: {
        "description": (
            "pipeline_jobs table -- backs the FastAPI layer's async job-trigger endpoint, "
            "so a slow pipeline.run() call (minutes, not seconds) can be started over HTTP "
            "and polled rather than blocking the request"
        ),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS pipeline_jobs (
                id              TEXT PRIMARY KEY,
                query           TEXT NOT NULL,
                options         TEXT,
                status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                stats           TEXT,
                error           TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at      TIMESTAMP,
                completed_at    TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON pipeline_jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_created ON pipeline_jobs(created_at)",
        ],
    },
    9: {
        "description": (
            "Commercial intelligence extension, part 1: extend "
            "supplier_capabilities.relationship's CHECK constraint to add 'asserted', for "
            "commercial claims (market presence, logistics facts) that have no "
            "in-house-vs-subcontracted distinction at all -- SQLite can't ALTER a CHECK "
            "constraint in place, so this rebuilds the table, the same pattern v4 already "
            "used for shipment_records.source"
        ),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS supplier_capabilities_v9 (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                reported_term       TEXT NOT NULL,
                canonical_term      TEXT,
                category            TEXT,
                relationship        TEXT NOT NULL CHECK (relationship IN ('in_house', 'subcontracted', 'asserted')),
                confidence          REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
                evidence            TEXT NOT NULL,
                source_url          TEXT,
                assessed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (supplier_id, reported_term, relationship)
            )
            """,
            """
            INSERT INTO supplier_capabilities_v9 (
                id, supplier_id, reported_term, canonical_term, category,
                relationship, confidence, evidence, source_url, assessed_at
            )
            SELECT
                id, supplier_id, reported_term, canonical_term, category,
                relationship, confidence, evidence, source_url, assessed_at
            FROM supplier_capabilities
            """,
            "DROP TABLE supplier_capabilities",
            "ALTER TABLE supplier_capabilities_v9 RENAME TO supplier_capabilities",
            "CREATE INDEX IF NOT EXISTS idx_cap_supplier ON supplier_capabilities(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_cap_canonical ON supplier_capabilities(canonical_term)",
        ],
    },
    10: {
        "description": (
            "Commercial intelligence extension, part 2: buyer_profiles (named, reusable "
            "search + commercial preference bundles) and procurement_outcomes (feedback-loop "
            "schema only, no UI, per spec)"
        ),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS buyer_profiles (
                id                              INTEGER PRIMARY KEY AUTOINCREMENT,
                name                            TEXT NOT NULL UNIQUE,
                destination_country             TEXT,
                required_capabilities           TEXT,
                preferred_incoterm              TEXT,
                preferred_payment_terms_days    INTEGER,
                min_company_size                TEXT,
                target_market                   TEXT,
                min_export_experience_years     INTEGER,
                manufacturers_only              BOOLEAN NOT NULL DEFAULT 1,
                created_at                      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS procurement_outcomes (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                buyer_profile_id    INTEGER REFERENCES buyer_profiles(id) ON DELETE SET NULL,
                outcome             TEXT NOT NULL,
                notes               TEXT,
                recorded_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_outcomes_supplier ON procurement_outcomes(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_outcomes_profile ON procurement_outcomes(buyer_profile_id)",
            "CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON procurement_outcomes(outcome)",
        ],
    },
    11: {
        "description": (
            "AI Discovery/Collection/Verification platform: ai_confidence_score and "
            "related columns on suppliers (deliberately separate from composite_score -- "
            "see verification_ai/), discovery/collection provenance columns, and four new "
            "history/evidence tables (verification_history, supplier_change_log, "
            "collection_runs, discovery_runs)"
        ),
        "columns": [
            ("suppliers", "ai_confidence_score", "INTEGER"),
            ("suppliers", "ai_confidence_assessed_at", "TIMESTAMP"),
            ("suppliers", "ai_summary", "TEXT"),
            ("suppliers", "ai_strengths", "TEXT"),
            ("suppliers", "ai_risks", "TEXT"),
            ("suppliers", "ai_suitable_customer_types", "TEXT"),
            ("suppliers", "ai_verification_model", "TEXT"),
            ("suppliers", "discovery_source", "TEXT"),
            ("suppliers", "collection_last_run_at", "TIMESTAMP"),
            ("suppliers", "collection_status", "TEXT"),
        ],
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS verification_history (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                verification_type   TEXT NOT NULL,
                confidence_score    INTEGER,
                verdict             TEXT,
                summary             TEXT,
                evidence_json       TEXT,
                model_used          TEXT,
                run_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_vh_supplier ON verification_history(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_vh_type ON verification_history(verification_type)",
            """
            CREATE TABLE IF NOT EXISTS supplier_change_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                field_name      TEXT NOT NULL,
                old_value       TEXT,
                new_value       TEXT,
                changed_by      TEXT NOT NULL,
                change_reason   TEXT,
                changed_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_scl_supplier ON supplier_change_log(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_scl_field ON supplier_change_log(field_name)",
            """
            CREATE TABLE IF NOT EXISTS collection_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                status          TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
                pages_visited   INTEGER NOT NULL DEFAULT 0,
                artifacts_dir   TEXT,
                proxy_provider  TEXT,
                error_message   TEXT,
                started_at      TIMESTAMP,
                completed_at    TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_cr_supplier ON collection_runs(supplier_id)",
            """
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                product_query           TEXT NOT NULL,
                category                TEXT,
                country                 TEXT,
                candidates_found        INTEGER NOT NULL DEFAULT 0,
                candidates_validated    INTEGER NOT NULL DEFAULT 0,
                candidates_rejected     INTEGER NOT NULL DEFAULT 0,
                candidates_duplicate    INTEGER NOT NULL DEFAULT 0,
                run_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_dr_query ON discovery_runs(product_query)",
        ],
    },
    12: {
        "description": (
            "Sourcing Agent: chat-driven spec-aware sourcing loop -- additive "
            "sourcing_* columns on suppliers (the detailed procurement-checklist "
            "dossier, deliberately separate from ai_summary/ai_confidence_score -- "
            "see sourcing/dossier_generator.py), sourcing_runs (one row per chat "
            "brief, scoping results + CSV download to that request), and "
            "pipeline_jobs.progress (live incremental status for a long-running "
            "sourcing run, polled the same way every other job's stats already is)"
        ),
        "columns": [
            ("suppliers", "sourcing_oem_odm_notes", "TEXT"),
            ("suppliers", "sourcing_factory_notes", "TEXT"),
            ("suppliers", "sourcing_engineering_notes", "TEXT"),
            ("suppliers", "sourcing_export_notes", "TEXT"),
            ("suppliers", "sourcing_volume_suitability", "TEXT"),
            ("suppliers", "sourcing_payment_terms_notes", "TEXT"),
            ("suppliers", "sourcing_verification_status", "TEXT"),
            ("pipeline_jobs", "progress", "TEXT"),
        ],
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS sourcing_runs (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_text                  TEXT NOT NULL,
                structured_brief_json       TEXT,
                target_count                INTEGER NOT NULL,
                examined_count              INTEGER NOT NULL DEFAULT 0,
                qualified_supplier_ids_json TEXT,
                status                      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
                error_message               TEXT,
                created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at                TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sruns_status ON sourcing_runs(status)",
        ],
    },
    13: {
        "description": (
            "Apollo named-contact discovery: key_contacts (JSON array of "
            "{name, title, email, phone, linkedin_url, role_category}) and "
            "contacts_found_at on suppliers -- a separate, opt-in enrichment "
            "stage (see verification/apollo_contact_finder.py and "
            "verification/contact_finder_service.py), never run automatically "
            "during a Sourcing Agent run"
        ),
        "columns": [
            ("suppliers", "key_contacts", "TEXT"),
            ("suppliers", "contacts_found_at", "TIMESTAMP"),
        ],
    },
    14: {
        "description": (
            "Procurement Decision Engine foundation: ai_confidence_breakdown "
            "(JSON array of {name, weight, verdict, contribution} -- see "
            "verification_ai/confidence_scorer.py's ConfidenceScoreResult) "
            "and procurement_recommendation/procurement_recommendation_reason "
            "(a deterministic 7-category RFQ-readiness recommendation -- see "
            "verification_ai/procurement_recommendation.py). "
            "procurement_recommendation deliberately has no DB CHECK "
            "constraint restricting it to its 7 valid values -- same reason "
            "ai_confidence_score has none (see its own SCHEMA_SQL comment): "
            "ALTER TABLE ADD COLUMN can't add one consistently with a "
            "fresh-DB CREATE TABLE, so this is validated by construction "
            "(only verification_ai/procurement_recommendation.py's fixed "
            "categoriser ever writes it) rather than at the DB layer."
        ),
        "columns": [
            ("suppliers", "ai_confidence_breakdown", "TEXT"),
            ("suppliers", "procurement_recommendation", "TEXT"),
            ("suppliers", "procurement_recommendation_reason", "TEXT"),
        ],
    },
    15: {
        "description": (
            "Procurement Decision Engine Phase 3: certificate_document_urls "
            "(JSON array of {url, matched_keyword, filename, artifact_path} -- "
            "see collection/site_collector.py's certificate detection+download, "
            "written by collection/collection_service.py alongside a normal "
            "collect() run, no separate opt-in stage) and production_lines_notes/"
            "machinery_notes/factory_ownership/factory_facts_extracted_at (a "
            "separate, opt-in LLM extraction stage -- see "
            "verification/factory_facts_extractor.py and "
            "verification/factory_facts_service.py). factory_ownership has no "
            "DB CHECK constraint (same reason as procurement_recommendation in "
            "v14) -- validated by the extractor's own soft-correction to "
            "'unclear' instead."
        ),
        "columns": [
            ("suppliers", "certificate_document_urls", "TEXT"),
            ("suppliers", "production_lines_notes", "TEXT"),
            ("suppliers", "machinery_notes", "TEXT"),
            ("suppliers", "factory_ownership", "TEXT"),
            ("suppliers", "factory_facts_extracted_at", "TIMESTAMP"),
        ],
    },
    16: {
        "description": (
            "Scoring engine rewrite (verification/scorer.py): replaces the flat "
            "verification/export/platform/contact weighting -- which read only "
            "near-empty China/Alibaba-shaped columns and structurally floored "
            "every non-Chinese, non-marketplace-listed supplier near zero -- "
            "with product_fit/provenance/verification/export/contact, demoting "
            "uscc_verified and Alibaba platform strength from weighted "
            "dimensions to small capped bonuses. Adds product_fit_score, "
            "provenance_score, evidence_coverage (0-100, same CHECK convention "
            "as the existing score columns) and 'unscored' as a valid "
            "recommendation value, for suppliers with too little evidence to "
            "judge either way (see verification.scorer.SupplierScorer._recommend). "
            "SQLite can't ALTER a CHECK constraint in place, so this rebuilds "
            "suppliers -- same DROP+RENAME pattern v4/v9 used, and SQLite does "
            "DROP a table it's PARENT to when foreign_keys is ON without an "
            "explicit PRAGMA foreign_keys=OFF bracket first -- verified by dry "
            "run: without it, DROP TABLE suppliers performs an implicit DELETE "
            "that fires every child table's ON DELETE CASCADE/SET NULL action, "
            "silently wiping dedup_candidates/supplier_capabilities/"
            "supplier_change_log and de-linking raw_source_data.golden_record_id "
            "for every row. With foreign_keys OFF during the rebuild and ids "
            "preserved verbatim by the INSERT...SELECT below, every child FK "
            "still resolves correctly once foreign_keys is re-enabled at the "
            "end (checked with PRAGMA foreign_key_check). platform_score is "
            "retained (still "
            "written by SupplierScorer._platform_score) rather than dropped, "
            "now feeding only that capped bonus instead of 20% of the weighted "
            "composite. A Python callable (see _rebuild_suppliers_v16 below), not "
            "static SQL statements like v4/v9 used -- two reasons: (1) PRAGMA "
            "foreign_keys is a documented no-op mid-transaction, and this loop's "
            "own schema_migrations INSERT after each prior migration leaves one "
            "open when several migrations apply in the same initialise_schema() "
            "call (e.g. upgrading a real pre-v4 database straight through to "
            "v16, exactly what tests/test_uk_trade_gap.py's v4 migration test "
            "does) -- so the OFF/ON pair needs conn.commit() brackets a static "
            "statement list can't express; (2) the INSERT...SELECT column list "
            "must be the intersection of what the OLD table actually has with "
            "the new shape, not a hardcoded full column list -- a real database "
            "can be mid-migration-history with fewer columns than current "
            "SCHEMA_SQL (see tests/test_phase1.py's "
            "test_v11_migration_upgrades_a_real_pre_v11_database, which "
            "reproduces exactly that against a deliberately minimal old "
            "suppliers table -- a hardcoded column list breaks with 'no such "
            "column' against it)."
        ),
        "python": ["_rebuild_suppliers_v16"],
    },
    17: {
        "description": (
            "suppliers.self_asserted_score -- verification/scorer.py's new "
            "self-asserted-verification bonus, computed from supplier_capabilities "
            "findings (a capability claim scraped from a supplier's own website) "
            "weighted by each finding's confidence. Deliberately kept separate from "
            "verification_score (independently-checked certificates/manufacturer "
            "status only) rather than blended into it -- a self-report and an "
            "independent check are different kinds of evidence, and collapsing them "
            "into one number would hide which one a given supplier actually has. No "
            "DB CHECK constraint -- same reason ai_confidence_score/"
            "procurement_recommendation/factory_ownership have none (see their own "
            "SCHEMA_SQL comments): ALTER TABLE ADD COLUMN can't add one consistently "
            "with a fresh-DB CREATE TABLE, so this is validated by construction "
            "(only SupplierScorer ever writes it, always clamped 0-100) rather than "
            "at the DB layer."
        ),
        "columns": [
            ("suppliers", "self_asserted_score", "INTEGER"),
        ],
    },
    18: {
        "description": (
            "CSV batch upload (batch/): batch_upload_rows (one row per uploaded "
            "CSV row, tracked separately from suppliers since a row may not "
            "resolve to one yet -- needs_url/needs_name -- and the original "
            "spreadsheet columns must survive untouched through to export "
            "regardless of enrichment outcome; batch_job_id is a pipeline_jobs.id, "
            "reusing the existing job-tracking/polling mechanism rather than a "
            "second job concept) and field_provenance (source_url/raw_snippet/"
            "extraction_method plus source_tier/claim_type -- the two inputs "
            "confidence is derived from, not a single pre-computed value, so the "
            "calibration report can group by source_tier independently of "
            "claim_type; only written for canonical_name -- the domain-derived-"
            "placeholder-replacement case -- as of this migration, extended to "
            "every other extracted field as part of the provenance feature proper)."
        ),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS batch_upload_rows (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_job_id        TEXT NOT NULL REFERENCES pipeline_jobs(id) ON DELETE CASCADE,
                row_index           INTEGER NOT NULL,
                original_columns    TEXT NOT NULL,
                company_name        TEXT,
                name_source          TEXT NOT NULL DEFAULT 'csv'
                                     CHECK (name_source IN ('csv', 'inferred_from_domain')),
                website               TEXT,
                status                TEXT NOT NULL DEFAULT 'pending'
                                     CHECK (status IN ('pending', 'needs_url', 'needs_name', 'processing', 'success', 'failed')),
                supplier_id            INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
                error_message          TEXT,
                created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_batch_rows_job ON batch_upload_rows(batch_job_id)",
            "CREATE INDEX IF NOT EXISTS idx_batch_rows_supplier ON batch_upload_rows(supplier_id)",
            """
            CREATE TABLE IF NOT EXISTS field_provenance (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                field_name          TEXT NOT NULL,
                value                 TEXT,
                source_url             TEXT,
                raw_snippet            TEXT,
                extraction_method      TEXT NOT NULL,
                source_tier             TEXT NOT NULL CHECK (source_tier IN ('own_domain', 'other')),
                claim_type               TEXT NOT NULL CHECK (claim_type IN ('verifiable_fact', 'self_assessed')),
                created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_field_provenance_supplier ON field_provenance(supplier_id)",
            "CREATE INDEX IF NOT EXISTS idx_field_provenance_field ON field_provenance(supplier_id, field_name)",
        ],
    },
}


# suppliers_v16's target shape -- the new table _rebuild_suppliers_v16 (below)
# creates before copying across whatever columns the old suppliers table
# actually has. Kept as its own statement (not folded into MIGRATIONS[16])
# so the CREATE TABLE text isn't buried inside a Python string concatenation.
_SUPPLIERS_V16_CREATE_SQL = """
            CREATE TABLE suppliers_v16 (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,

                canonical_name          TEXT NOT NULL,
                aliases                 TEXT,
                domain                  TEXT UNIQUE,

                country                 TEXT,
                province_state          TEXT,
                city                    TEXT,
                address                 TEXT,

                primary_email           TEXT,
                secondary_emails        TEXT,
                primary_phone           TEXT,
                whatsapp                TEXT,
                wechat_id               TEXT,
                linkedin_url            TEXT,
                contact_name            TEXT,
                contact_title           TEXT,

                uscc                    TEXT UNIQUE,
                uscc_verified           BOOLEAN NOT NULL DEFAULT 0,
                uscc_verified_at        TIMESTAMP,
                company_reg_number      TEXT,
                is_manufacturer         BOOLEAN,
                manufacturer_confidence INTEGER NOT NULL DEFAULT 0
                                        CHECK (manufacturer_confidence BETWEEN 0 AND 100),
                manufacturer_signals    TEXT,
                manufacturer_verified_at TIMESTAMP,
                capability_extracted_at TIMESTAMP,
                website_search_attempted_at TIMESTAMP,
                facility_address_verified BOOLEAN,
                facility_address_verification_source TEXT,
                facility_address_verified_at TIMESTAMP,
                linkedin_checked_at TIMESTAMP,
                contact_form_url TEXT,
                registered_capital_rmb  REAL,
                business_scope          TEXT,
                factory_photo_urls      TEXT,
                factory_photo_verdict   TEXT,
                factory_photo_assessed_at TIMESTAMP,
                year_established        INTEGER,
                employee_count          TEXT,
                factory_size_sqm        INTEGER,
                annual_revenue_usd      TEXT,

                iso_9001                BOOLEAN NOT NULL DEFAULT 0,
                iso_9001_expiry         DATE,
                iso_ts_16949            BOOLEAN NOT NULL DEFAULT 0,
                e_mark_certified        BOOLEAN NOT NULL DEFAULT 0,
                e_mark_numbers          TEXT,
                ce_certified            BOOLEAN NOT NULL DEFAULT 0,
                ukca_certified          BOOLEAN NOT NULL DEFAULT 0,
                iatf_16949              BOOLEAN NOT NULL DEFAULT 0,
                other_certifications    TEXT,

                primary_categories      TEXT,
                product_keywords        TEXT,
                trailer_components      TEXT,
                moq_notes               TEXT,

                exports_to_uk           BOOLEAN NOT NULL DEFAULT 0,
                exports_to_eu           BOOLEAN NOT NULL DEFAULT 0,
                exports_to_us           BOOLEAN NOT NULL DEFAULT 0,
                active_export_countries TEXT,
                confirmed_shipments_uk  INTEGER NOT NULL DEFAULT 0,
                confirmed_shipments_eu  INTEGER NOT NULL DEFAULT 0,
                confirmed_shipments_us  INTEGER NOT NULL DEFAULT 0,
                last_shipment_date      DATE,
                annual_export_volume    TEXT,
                known_buyers            TEXT,
                sinosure_coverage       BOOLEAN,

                payment_terms_offered   TEXT,
                incoterms_supported     TEXT,
                currencies_accepted     TEXT,
                can_do_ddp_uk           BOOLEAN NOT NULL DEFAULT 0,

                alibaba_url             TEXT,
                alibaba_gold_supplier   BOOLEAN NOT NULL DEFAULT 0,
                alibaba_years           INTEGER,
                alibaba_trade_assurance BOOLEAN NOT NULL DEFAULT 0,
                alibaba_rating          REAL,
                indiamart_url           TEXT,
                hktdc_url               TEXT,
                made_in_china_url       TEXT,

                product_fit_score        INTEGER NOT NULL DEFAULT 0
                                         CHECK (product_fit_score BETWEEN 0 AND 100),
                provenance_score         INTEGER NOT NULL DEFAULT 0
                                         CHECK (provenance_score BETWEEN 0 AND 100),
                verification_score      INTEGER NOT NULL DEFAULT 0
                                         CHECK (verification_score BETWEEN 0 AND 100),
                export_score             INTEGER NOT NULL DEFAULT 0
                                         CHECK (export_score BETWEEN 0 AND 100),
                platform_score           INTEGER NOT NULL DEFAULT 0
                                         CHECK (platform_score BETWEEN 0 AND 100),
                contact_score             INTEGER NOT NULL DEFAULT 0
                                         CHECK (contact_score BETWEEN 0 AND 100),
                composite_score           INTEGER NOT NULL DEFAULT 0
                                         CHECK (composite_score BETWEEN 0 AND 100),
                evidence_coverage        INTEGER NOT NULL DEFAULT 0
                                         CHECK (evidence_coverage BETWEEN 0 AND 100),
                recommendation            TEXT NOT NULL DEFAULT 'unverified'
                                         CHECK (recommendation IN
                                             ('recommended', 'review', 'unverified', 'unscored', 'avoid')),

                source_count             INTEGER NOT NULL DEFAULT 1,
                first_seen                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_updated               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_verified               TIMESTAMP,
                notes                        TEXT,
                flagged                      BOOLEAN NOT NULL DEFAULT 0,
                flag_reason                  TEXT,

                ai_confidence_score          INTEGER,
                ai_confidence_assessed_at    TIMESTAMP,
                ai_summary                   TEXT,
                ai_strengths                 TEXT,
                ai_risks                     TEXT,
                ai_suitable_customer_types   TEXT,
                ai_verification_model        TEXT,

                discovery_source              TEXT,
                collection_last_run_at        TIMESTAMP,
                collection_status             TEXT,

                sourcing_oem_odm_notes         TEXT,
                sourcing_factory_notes         TEXT,
                sourcing_engineering_notes     TEXT,
                sourcing_export_notes          TEXT,
                sourcing_volume_suitability    TEXT,
                sourcing_payment_terms_notes   TEXT,
                sourcing_verification_status   TEXT,

                key_contacts                   TEXT,
                contacts_found_at              TIMESTAMP,

                ai_confidence_breakdown        TEXT,
                procurement_recommendation     TEXT,
                procurement_recommendation_reason TEXT,

                certificate_document_urls      TEXT,
                production_lines_notes         TEXT,
                machinery_notes                TEXT,
                factory_ownership               TEXT,
                factory_facts_extracted_at     TIMESTAMP
            )
            """

_SUPPLIERS_V16_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_sup_domain ON suppliers(domain)",
    "CREATE INDEX IF NOT EXISTS idx_sup_uscc ON suppliers(uscc)",
    "CREATE INDEX IF NOT EXISTS idx_sup_country ON suppliers(country)",
    "CREATE INDEX IF NOT EXISTS idx_sup_score ON suppliers(composite_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sup_recommendation ON suppliers(recommendation)",
    "CREATE INDEX IF NOT EXISTS idx_sup_e_mark ON suppliers(e_mark_certified)",
    "CREATE INDEX IF NOT EXISTS idx_sup_manufacturer ON suppliers(is_manufacturer)",
    "CREATE INDEX IF NOT EXISTS idx_sup_canonical_name ON suppliers(canonical_name)",
)


def _rebuild_suppliers_v16(conn: sqlite3.Connection) -> None:
    """See MIGRATIONS[16]'s description for the full reasoning. Two
    things a static "statements" list can't express, which is why this
    is a Python callable instead:

    1. PRAGMA foreign_keys is a documented SQLite no-op when set inside
       an open transaction -- and this migration runner's own "INSERT
       INTO schema_migrations" after each prior migration leaves one
       open whenever several migrations apply in a single
       initialise_schema() call. conn.commit() first guarantees the
       OFF (and later the ON) actually takes effect.
    2. The INSERT...SELECT column list is the intersection of the OLD
       table's actual columns with the new shape, discovered via
       PRAGMA table_info at migration time -- not a hardcoded full
       column list, which breaks with "no such column" against a real
       database that's behind current SCHEMA_SQL (mid-migration-history
       production data, or a test fixture reconstructing an old shape).
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(_SUPPLIERS_V16_CREATE_SQL)

    old_columns = [row["name"] for row in conn.execute("PRAGMA table_info(suppliers)").fetchall()]
    new_columns = {row["name"] for row in conn.execute("PRAGMA table_info(suppliers_v16)").fetchall()}
    common_columns = [c for c in old_columns if c in new_columns]
    col_list = ", ".join(common_columns)
    conn.execute(f"INSERT INTO suppliers_v16 ({col_list}) SELECT {col_list} FROM suppliers")

    conn.execute("DROP TABLE suppliers")
    conn.execute("ALTER TABLE suppliers_v16 RENAME TO suppliers")
    for statement in _SUPPLIERS_V16_INDEX_STATEMENTS:
        conn.execute(statement)

    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


# Migration entries may reference a Python callable by name (string) in a
# "python" list -- resolved here rather than at MIGRATIONS-definition time
# since the callables (e.g. _rebuild_suppliers_v16 above) are defined after
# MIGRATIONS in this module for readability.
_MIGRATION_FUNCTIONS = {
    "_rebuild_suppliers_v16": _rebuild_suppliers_v16,
}


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        logger.info("Migration: added %s.%s (%s)", table, column, col_type)


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """
    Open a new SQLite connection with sane production defaults:
    - Row factory so results behave like dicts (`row["col"]`)
    - Foreign key enforcement ON (off by default in SQLite)
    - WAL journal mode for better concurrent read/write behaviour
    - A busy timeout so concurrent writers back off instead of
      immediately raising 'database is locked'
    """
    path = Path(db_path) if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
    except sqlite3.OperationalError as e:
        # sqlite3's own "unable to open database file" gives no indication
        # of *why* — no path, no permission info, nothing actionable. This
        # is especially common on Windows (cloud-synced folders like
        # OneDrive-backed Downloads/Documents, antivirus real-time
        # scanning, or restrictive folder permissions can all produce
        # exactly this error even when the directory visibly exists).
        # Re-raise with the actual resolved path and a concrete diagnosis
        # so the person doesn't have to guess.
        resolved = path.resolve()
        diagnosis = []
        if not resolved.parent.exists():
            diagnosis.append(f"parent directory does not exist: {resolved.parent}")
        elif not os.access(resolved.parent, os.W_OK):
            diagnosis.append(f"parent directory is not writable: {resolved.parent}")
        else:
            diagnosis.append(
                "parent directory exists and appears writable — this is likely "
                "OS-level interference (a cloud-synced folder like OneDrive, "
                "antivirus real-time scanning, or a restrictive permission "
                "policy), not a problem with this codebase's path logic. Try "
                "moving the project to a plain local folder (not inside "
                "OneDrive/Dropbox/Google Drive sync) and re-running."
            )
        raise sqlite3.OperationalError(
            f"{e} — attempted path: {resolved}. {' '.join(diagnosis)}"
        ) from e

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


@contextmanager
def connection_scope(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success, rolls back on exception,
    and always closes the connection. Prefer this over calling
    get_connection() directly outside of Database/Repository internals."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialise_schema(db_path: Path | str | None = None) -> None:
    """Create all tables/indexes if they don't already exist (via
    SCHEMA_SQL — every statement there is idempotent), then apply any
    incremental migrations from MIGRATIONS that haven't been recorded
    yet. Safe to call repeatedly, and safe to call against either a
    brand-new database or one created under an older SCHEMA_VERSION."""
    with connection_scope(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

        applied_versions = {
            row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }

        if 1 not in applied_versions:
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (1, "Initial Phase 1 schema: suppliers, raw_source_data, "
                    "shipment_records, dedup_candidates, search_log"),
            )
            applied_versions.add(1)
            logger.info("Recorded schema migration v1")

        for version in sorted(MIGRATIONS):
            if version in applied_versions:
                continue
            migration = MIGRATIONS[version]
            for table, column, col_type in migration.get("columns", []):
                _add_column_if_missing(conn, table, column, col_type)
            for statement in migration.get("statements", []):
                conn.execute(statement)
            for func_name in migration.get("python", []):
                _MIGRATION_FUNCTIONS[func_name](conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (version, migration["description"]),
            )
            logger.info("Applied schema migration v%s: %s", version, migration["description"])


def get_schema_version(db_path: Path | str | None = None) -> int:
    """Return the highest applied migration version, or 0 if the
    migrations table doesn't exist yet (fresh/uninitialised DB)."""
    with connection_scope(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        return row["v"] or 0


def table_counts(db_path: Path | str | None = None) -> dict[str, int]:
    """Return row counts for every core table — used by `main.py status`
    and by tests to sanity-check the schema was created correctly."""
    tables = (
        "suppliers",
        "raw_source_data",
        "shipment_records",
        "dedup_candidates",
        "search_log",
        "source_query_runs",
        "supplier_capabilities",
        "pipeline_jobs",
        "buyer_profiles",
        "procurement_outcomes",
        "verification_history",
        "supplier_change_log",
        "collection_runs",
        "discovery_runs",
        "schema_migrations",
    )
    counts: dict[str, int] = {}
    with connection_scope(db_path) as conn:
        for table in tables:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = row["n"]
    return counts
