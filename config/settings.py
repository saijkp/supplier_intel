"""
config/settings.py

Central configuration for the Supplier Intelligence Platform.
All paths, API keys, constants, HS codes, scoring weights, and
thresholds live here so nothing is hardcoded downstream.
"""

from __future__ import annotations

import os
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# Load environment variables from .env (never committed)
# ─────────────────────────────────────────────────────────────
load_dotenv()


# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = Path(os.getenv("SUPPLIER_INTEL_DB_PATH") or str(DATA_DIR / "suppliers.db"))
# ^ deliberately `os.getenv(key) or default`, not `os.getenv(key, default)`.
# The two-argument form only falls back when the key is *absent* from the
# environment -- it does NOT fall back when the key is present but blank,
# which is exactly what happens if a .env file has "SUPPLIER_INTEL_DB_PATH="
# with nothing after the "=" (python-dotenv loads that as an empty string,
# not as unset). An empty string here becomes Path("").resolve(), which
# resolves to the current working directory -- so sqlite3 ends up trying
# to open the project folder itself as a database file, producing a
# confusing "unable to open database file" error that looks like OS/
# permissions interference but is actually just this. Confirmed directly:
# a real user hit exactly this from a blank line in their own .env.
LOG_DIR = BASE_DIR / "logs"

# collection/'s HTML/screenshot/PDF/JSON artifacts -- same
# `os.getenv(key) or default` pattern as DB_PATH above, and for the
# same reason: on Railway only the `/data` volume mount survives a
# redeploy (see DEPLOY.md), and DATA_DIR itself has no env override,
# so without this Collection Service's artifacts would be silently
# wiped on every deploy.
COLLECTION_ARTIFACTS_DIR = Path(os.getenv("COLLECTION_ARTIFACTS_DIR") or str(DATA_DIR / "collection"))

# Ensure critical directories exist at import time. This keeps main.py
# and tests from failing on a fresh checkout before init-db has run.
for _dir in (DATA_DIR, EXPORTS_DIR, LOG_DIR, COLLECTION_ARTIFACTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# API Keys (loaded from environment / .env — never hardcode)
# ─────────────────────────────────────────────────────────────
APIFY_TOKEN: str | None = os.getenv("APIFY_TOKEN")
QICHACHA_API_KEY: str | None = os.getenv("QICHACHA_API_KEY")
# Qichacha's signed endpoints require an AppKey + AppSecret pair (the
# AppKey is QICHACHA_API_KEY above; the secret signs each request).
QICHACHA_SECRET_KEY: str | None = os.getenv("QICHACHA_SECRET_KEY")
SERPAPI_KEY: str | None = os.getenv("SERPAPI_KEY")
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
GOOGLE_PLACES_API_KEY: str | None = os.getenv("GOOGLE_PLACES_API_KEY")
# Registration is genuinely harder to get than the other keys here as
# a non-Chinese developer/company -- see
# verification/facility_address_verifier.py's module docstring before
# budgeting time for this one.
AMAP_API_KEY: str | None = os.getenv("AMAP_API_KEY")
# Apollo.io -- named Procurement/Sales/CEO contact discovery, see
# verification/apollo_contact_finder.py's module docstring for the
# search-is-free/enrichment-costs-credits split this key is used for.
# Apollo's people-search endpoint documents itself as requiring a
# "master" API key specifically -- confirm the configured key has that
# permission level if search calls come back empty/rejected.
APOLLO_API_KEY: str | None = os.getenv("APOLLO_API_KEY")
# UK Companies House -- verification/companies_house_client.py. Used as
# HTTP Basic Auth username with a blank password (Companies House's own
# scheme, not a bug) -- see that module's own docstring.
COMPANIES_HOUSE_API_KEY: str | None = os.getenv("COMPANIES_HOUSE_API_KEY")

# --- Contact Finder Service (verification/contact_finder_service.py) ----
# Same wall-clock-budget / process-wide-semaphore safeguards as
# Collection Service below, applied to Apollo contact lookup batches.
CONTACTS_JOB_MAX_SECONDS: int = int(os.getenv("CONTACTS_JOB_MAX_SECONDS") or 600)
CONTACTS_MAX_CONCURRENT_JOBS: int = int(os.getenv("CONTACTS_MAX_CONCURRENT_JOBS") or 1)

# --- UK Company Verification Service (verification/uk_company_verification_service.py) ---
# Same wall-clock-budget / process-wide-semaphore safeguards, applied to
# Companies House lookup batches -- CLI-only for now, no async job/API
# endpoint (see that module's own docstring).
UK_VERIFICATION_JOB_MAX_SECONDS: int = int(os.getenv("UK_VERIFICATION_JOB_MAX_SECONDS") or 600)
UK_VERIFICATION_MAX_CONCURRENT_JOBS: int = int(os.getenv("UK_VERIFICATION_MAX_CONCURRENT_JOBS") or 1)

# --- Collection Service (collection/) -- Playwright + rotating proxies ---
# Webshare is the first (and, for now, only implemented) rotating-proxy
# provider -- see collection/proxy_provider.py's module docstring for
# why: a genuine free tier to build/test against, cheapest paid tier of
# the five named in the original brief. Bright Data/Oxylabs/Decodo/
# IPRoyal are documented extension points, not implemented yet.
WEBSHARE_PROXY_USERNAME: str | None = os.getenv("WEBSHARE_PROXY_USERNAME")
WEBSHARE_PROXY_PASSWORD: str | None = os.getenv("WEBSHARE_PROXY_PASSWORD")
WEBSHARE_PROXY_ENDPOINT: str = os.getenv("WEBSHARE_PROXY_ENDPOINT") or "p.webshare.io:80"
COLLECTION_PROXY_PROVIDER: str = os.getenv("COLLECTION_PROXY_PROVIDER") or "none"  # 'webshare' | 'none'

COLLECTION_PAGE_TIMEOUT_MS: int = int(os.getenv("COLLECTION_PAGE_TIMEOUT_MS") or 25_000)
# Certificate PDF/doc detection + download during collection (Procurement
# Decision Engine Phase 3) -- "free" evidence off an already-open browser
# session (a few extra context.request.get() calls, no new paid API), so
# this rides along inside collect()/collect_pending() rather than needing
# its own opt-in flag. Capped to bound cost/storage per supplier.
MAX_CERTIFICATE_DOWNLOADS: int = int(os.getenv("MAX_CERTIFICATE_DOWNLOADS") or 5)
# Wall-clock budget per collect_pending() batch call -- BackgroundTasks
# has no built-in timeout (unlike a real task queue), so this is
# self-enforced: a batch that runs past budget stops early and marks
# itself partial, safely resumable on the next call.
COLLECTION_JOB_MAX_SECONDS: int = int(os.getenv("COLLECTION_JOB_MAX_SECONDS") or 1200)
COLLECTION_MAX_CONCURRENT_JOBS: int = int(os.getenv("COLLECTION_MAX_CONCURRENT_JOBS") or 1)
# Suppliers collected concurrently WITHIN one collect_pending() batch call
# (distinct from COLLECTION_MAX_CONCURRENT_JOBS above, which caps
# concurrent BATCH calls, not per-item parallelism). Each unit of work
# launches its own headless Chromium process -- kept small and
# conservative by default given the real memory pressure this creates
# on a modest Railway plan; see collection/collection_service.py's
# module docstring for the full reasoning.
COLLECTION_PARALLEL_WORKERS: int = int(os.getenv("COLLECTION_PARALLEL_WORKERS") or 3)

# --- Sourcing Agent (sourcing/sourcing_agent.py) -------------------------
# Candidates processed concurrently within one _process_batch() wave --
# same reasoning/default as COLLECTION_PARALLEL_WORKERS above, since
# each candidate's processing includes a real Collection Service call.
SOURCING_PARALLEL_WORKERS: int = int(os.getenv("SOURCING_PARALLEL_WORKERS") or 3)

# --- Factory Facts Service (verification/factory_facts_service.py) ------
# Same wall-clock-budget / process-wide-semaphore safeguards as
# Contact Finder Service, applied to factory-facts extraction batches
# (a separate, opt-in, per-supplier OpenAI call -- see
# verification/factory_facts_extractor.py).
FACTORY_FACTS_JOB_MAX_SECONDS: int = int(os.getenv("FACTORY_FACTS_JOB_MAX_SECONDS") or 600)
FACTORY_FACTS_MAX_CONCURRENT_JOBS: int = int(os.getenv("FACTORY_FACTS_MAX_CONCURRENT_JOBS") or 1)

# --- Monitoring Service (monitoring/monitoring_service.py) --
# Same wall-clock-budget / process-wide-semaphore safeguards, applied to
# supplier_snapshots re-check batches. All v1 tracked fields are free
# (no paid API calls) -- see monitoring_service.py's own module
# docstring for why that does NOT mean future fields inherit the same
# no-confirmation-needed default.
MONITORING_JOB_MAX_SECONDS: int = int(os.getenv("MONITORING_JOB_MAX_SECONDS") or 600)
MONITORING_MAX_CONCURRENT_JOBS: int = int(os.getenv("MONITORING_MAX_CONCURRENT_JOBS") or 1)

# --- Catalogue Depth Service (verification/catalogue_depth_service.py) --
# Same wall-clock-budget / process-wide-semaphore safeguards, applied to
# catalogue-depth-signal extraction batches (a separate, opt-in,
# per-supplier OpenAI call over already-collected pages -- see
# verification/catalogue_depth_extractor.py).
CATALOGUE_DEPTH_JOB_MAX_SECONDS: int = int(os.getenv("CATALOGUE_DEPTH_JOB_MAX_SECONDS") or 600)
CATALOGUE_DEPTH_MAX_CONCURRENT_JOBS: int = int(os.getenv("CATALOGUE_DEPTH_MAX_CONCURRENT_JOBS") or 1)

# --- FastAPI layer (api/app.py) -----------------------------------------
# A single shared-secret token, not per-user auth -- see api/auth.py's
# own module docstring for why that's a deliberate v1 choice, not an
# oversight. Deliberately no default: api/auth.py fails closed (503)
# rather than silently serving unauthenticated requests when this is
# unset.
API_ACCESS_TOKEN: str | None = os.getenv("API_ACCESS_TOKEN")

# Comma-separated list of origins allowed to call the API from a
# browser (e.g. your Netlify frontend's URL). Empty by default --
# CORS denies everything until you explicitly add your frontend's
# origin, which is the safe default for a not-yet-public API.
ALLOWED_ORIGINS: list[str] = [
    origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()
]

REQUIRED_API_KEYS_FOR_LIVE_SCRAPING = (
    "APIFY_TOKEN",
    "QICHACHA_API_KEY",
    "QICHACHA_SECRET_KEY",
)


def missing_api_keys() -> List[str]:
    """Return names of required keys that are not set. Useful for a
    'doctor'/status command so setup problems surface early."""
    missing = []
    for key_name in REQUIRED_API_KEYS_FOR_LIVE_SCRAPING:
        if not os.getenv(key_name):
            missing.append(key_name)
    return missing


# ─────────────────────────────────────────────────────────────
# Apify Actor IDs
# ─────────────────────────────────────────────────────────────
APIFY_ACTORS: Dict[str, str | None] = {
    # curious_coder/alibaba-scraper was confirmed dead on Apify's own
    # platform (a real "Actor with this name was not found" from their
    # public API, not an auth/visibility issue) -- replaced with
    # zen-studio/alibaba-scraper, actively maintained (last build
    # 2026-07-27, 98% success rate over the last 30 days at the time
    # this was checked). See scrapers/alibaba_scraper.py's own
    # docstring for the input/output schema differences this required.
    "alibaba": "zen-studio/alibaba-scraper",
    "indiamart": "zuzka_mach/indiamart-scraper",  # also confirmed dead -- not yet replaced, see README
    "hktdc": None,  # Custom scraper, not run via Apify
    "google": "apify/google-search-scraper",
    # 1688.com -- Alibaba's domestic Chinese wholesale platform, distinct
    # from Alibaba.com. Evaluated as a second discovery source alongside
    # zen-studio/alibaba-scraper specifically because 1688 sellers skew
    # more toward genuine manufacturers than Alibaba.com's international-
    # trade mix. See scrapers/scraper_1688.py's own module docstring.
    "china_1688": "webdata_labs/1688-scraper",
}


# ─────────────────────────────────────────────────────────────
# Trailer Component HS Codes
# ─────────────────────────────────────────────────────────────
TRAILER_HS_CODES: List[str] = [
    "8716",     # Trailers and semi-trailers
    "8708",     # Parts and accessories for motor vehicles
    "8539",     # Electric filament/discharge lamps (LED)
    "7318",     # Screws, bolts, nuts, fasteners
    "4016",     # Rubber articles (seals, grommets)
    "8716901",  # Parts of trailers
    "9401",     # Seats (horsebox interiors)
]

# Product category taxonomy used for search matching and reporting.
# Kept here (rather than a separate categories.py) for Phase 1 simplicity;
# split out once the list grows past what's comfortable in one file.
PRODUCT_CATEGORIES: List[str] = [
    "LED Lighting",
    "Fasteners",
    "Axles & Suspension",
    "Couplings & Towing",
    "Brakes & Braking Systems",
    "Chassis & Frames",
    "Panels & Cladding",
    "Rubber Seals & Grommets",
    "Wiring & Electrics",
    "Hitches & Jockey Wheels",
    "Wheels & Tyres",
    "Ramps & Doors",
]

# A starting classification, not an authority — a buyer's own
# knowledge of UK/EU trailer type-approval requirements should
# override this freely. "Safety-critical" isn't a certification a
# supplier holds (E-mark and ISO 9001 are, and are the real, checkable
# facts — see verification/capability_vocabulary.py); it's a property
# of the *part*, used here purely as an informational flag on search
# results so a buyer knows to look harder at the certification
# evidence for these categories specifically, never as a filter or a
# substitute for actually checking the relevant cert.
SAFETY_CRITICAL_CATEGORIES: set = {
    "LED Lighting",           # E-mark / ECE R48 relevant
    "Axles & Suspension",
    "Couplings & Towing",
    "Brakes & Braking Systems",
    "Chassis & Frames",       # structural
    "Hitches & Jockey Wheels",
    "Wheels & Tyres",
}

# Specific, literal search terms for sweeping the pipeline across the
# full trailer-component catalogue (pipeline.orchestrator's sweep/
# campaign mode) — a broad category label like "Axles & Suspension"
# makes a poor search query on Alibaba/HKTDC/IndiaMART directly; a
# specific term like "trailer torsion axle" performs far better. Grouped
# by PRODUCT_CATEGORIES above but flattened here since campaign mode
# just needs one query list. Not exhaustive — extend as real sourcing
# needs surface more specific component names.
# Structured taxonomy distinguishing complete ASSEMBLIES (what a
# trailer builder actually buys and bolts on) from sub-COMPONENTS (what
# goes inside an assembly, e.g. a replacement bulb). This distinction
# exists specifically because "trailer lights" as a bare search term is
# genuinely ambiguous between the two, and B2B platforms tend to rank
# cheap, high-volume bulb listings above the complete assembly a buyer
# usually actually wants — searching the vague term alone reliably
# surfaces bulbs, not housings. Prefer TRAILER_COMPONENT_TAXONOMY over
# the flat TRAILER_COMPONENT_SEARCH_TERMS list below when precision
# matters; use get_search_terms(level="assembly") to search for
# complete units only.
#
# Only Lighting is built out with this assembly/component split in full
# — that's the category the ambiguity was actually reported in. The
# pattern (a "complete unit" search term distinct from a "just the
# consumable/wear part" term) applies just as well to Brakes (caliper
# assembly vs. brake pads), Couplings (full coupling head vs. pins/
# springs), etc. — extending it is a good next increment, not done
# exhaustively here.
TRAILER_COMPONENT_TAXONOMY: Dict[str, Dict[str, Any]] = {
    "rear_combination_lamp": {
        "category": "LED Lighting", "level": "assembly",
        "search_terms": [
            "trailer rear combination lamp assembly",
            "trailer tail light complete unit with housing",
            "trailer rear light assembly",
        ],
    },
    "marker_light_assembly": {
        "category": "LED Lighting", "level": "assembly",
        "search_terms": [
            "trailer LED marker light assembly",
            "trailer side marker lamp complete unit",
        ],
    },
    "number_plate_light_assembly": {
        "category": "LED Lighting", "level": "assembly",
        "search_terms": ["trailer number plate light assembly"],
    },
    "led_replacement_bulb": {
        "category": "LED Lighting", "level": "component",
        "search_terms": [
            "LED replacement bulb trailer light",
            "trailer light bulb LED",
        ],
    },
    "lamp_lens_cover": {
        "category": "LED Lighting", "level": "component",
        "search_terms": ["trailer lamp lens cover replacement"],
    },
    "trailer_axle_assembly": {
        "category": "Axles & Suspension", "level": "assembly",
        "search_terms": ["trailer axle assembly complete", "trailer torsion axle"],
    },
    "leaf_spring_suspension": {
        "category": "Axles & Suspension", "level": "component",
        "search_terms": ["leaf spring trailer suspension"],
    },
    "coupling_head_assembly": {
        "category": "Couplings & Towing", "level": "assembly",
        "search_terms": ["trailer coupling head assembly", "trailer tow hitch complete unit"],
    },
    "brake_drum_assembly": {
        "category": "Brakes & Braking Systems", "level": "assembly",
        "search_terms": ["trailer brake drum assembly"],
    },
    "brake_caliper": {
        "category": "Brakes & Braking Systems", "level": "component",
        "search_terms": ["trailer brake caliper"],
    },
}


def get_search_terms(level: Optional[str] = None, category: Optional[str] = None) -> List[str]:
    """Flatten TRAILER_COMPONENT_TAXONOMY into a search-term list,
    optionally filtered to level='assembly'|'component' and/or a
    specific category. get_search_terms(level='assembly') is the fix
    for the bulb-vs-housing ambiguity: it only returns terms for
    complete units, never bare consumable/wear parts."""
    terms: List[str] = []
    for entry in TRAILER_COMPONENT_TAXONOMY.values():
        if level and entry["level"] != level:
            continue
        if category and entry["category"] != category:
            continue
        terms.extend(entry["search_terms"])
    return terms


# Flat list used by pipeline.orchestrator.run_campaign's default sweep.
# Kept as a superset (assemblies + components + the original broader
# terms) for backward compatibility with existing sweeps; pass
# queries=get_search_terms(level="assembly") explicitly when you
# specifically want complete units only.
TRAILER_COMPONENT_SEARCH_TERMS: List[str] = [
    # LED Lighting — deliberately split assembly vs. component (see
    # TRAILER_COMPONENT_TAXONOMY): "LED trailer tail light" alone is the
    # exact term that returned bulbs instead of the complete unit.
    "trailer rear combination lamp assembly",
    "trailer tail light complete unit with housing",
    "LED trailer marker light assembly",
    "LED trailer number plate light assembly",
    "LED replacement bulb trailer light",  # component — kept for buyers who DO want just the bulb
    # Fasteners
    "trailer hex bolt",
    "trailer U-bolt",
    "zinc plated trailer fastener",
    # Axles & Suspension
    "trailer axle assembly complete",
    "leaf spring trailer suspension",
    "trailer torsion axle",
    # Couplings & Towing
    "trailer coupling head assembly",
    "trailer tow hitch complete unit",
    "trailer drawbar",
    # Brakes & Braking Systems
    "trailer brake drum assembly",
    "trailer brake caliper",
    "trailer electric brake",
    # Chassis & Frames
    "trailer chassis frame",
    "box trailer frame",
    # Panels & Cladding
    "trailer floor panel",
    "GRP trailer panel",
    "aluminium trailer cladding",
    # Rubber Seals & Grommets
    "trailer door seal",
    "rubber mudguard",
    "trailer wiring grommet",
    # Wiring & Electrics
    "trailer wiring harness",
    "7 pin trailer plug",
    "trailer junction box",
    # Hitches & Jockey Wheels
    "trailer jockey wheel",
    "trailer hitch lock",
    # Wheels & Tyres
    "trailer wheel rim",
    "trailer tyre",
    # Ramps & Doors
    "trailer loading ramp",
    "trailer tailgate",
    "horsebox ramp",
]


# ─────────────────────────────────────────────────────────────
# Data source identifiers (single source of truth for the
# 'source' column values used across raw_source_data / shipment_records)
# ─────────────────────────────────────────────────────────────
VALID_SOURCES: Tuple[str, ...] = (
    "alibaba",
    "indiamart",
    "hktdc",
    "shanghai_expo",
    "panjiva",
    "importyeti",
    "china_1688",
)

VALID_SHIPMENT_SOURCES: Tuple[str, ...] = ("panjiva", "importyeti", "volza")

VALID_PROCESSING_STATUSES: Tuple[str, ...] = (
    "pending",
    "processed",
    "failed",
    "duplicate",
)

VALID_DEDUP_STATUSES: Tuple[str, ...] = ("pending", "merged", "rejected")

VALID_RECOMMENDATIONS: Tuple[str, ...] = (
    "recommended",
    "review",
    "unverified",
    "unscored",
    "avoid",
)


# ─────────────────────────────────────────────────────────────
# Scoring Weights (must sum to 1.0 — enforced by an assertion below
# so a typo here fails loudly at import time rather than silently
# skewing every composite score)
#
# verification.scorer.SupplierScorer's five weighted dimensions.
# USCC verification and Alibaba platform strength are deliberately
# NOT weighted dimensions here -- they're small capped bonuses added
# on top of the composite instead, so a supplier with no Chinese
# marketplace presence (most non-Chinese manufacturers) never has
# points it structurally cannot reach.
# ─────────────────────────────────────────────────────────────
SCORING_WEIGHTS: Dict[str, float] = {
    "product_fit": 0.25,    # product_keywords matched against reports.coverage.BOM_CATEGORIES
    "provenance": 0.25,     # source quality/corroboration, live domain, real address
    "verification": 0.25,   # confirmed manufacturer, ISO 9001, E-mark, IATF/TS 16949
    "export": 0.15,         # shipment history
    "contact": 0.10,        # quality of contact info
}

_weight_sum = round(sum(SCORING_WEIGHTS.values()), 6)
assert _weight_sum == 1.0, (
    f"SCORING_WEIGHTS must sum to 1.0, got {_weight_sum}. "
    "Fix config/settings.py before running the pipeline."
)


# ─────────────────────────────────────────────────────────────
# Deduplication Thresholds
# ─────────────────────────────────────────────────────────────
DEDUP_AUTO_MERGE_THRESHOLD: float = 0.92
DEDUP_HUMAN_REVIEW_THRESHOLD: float = 0.75
DEDUP_REJECT_THRESHOLD: float = 0.50

assert (
    DEDUP_REJECT_THRESHOLD
    < DEDUP_HUMAN_REVIEW_THRESHOLD
    < DEDUP_AUTO_MERGE_THRESHOLD
    <= 1.0
), "Dedup thresholds must be strictly increasing and <= 1.0"


# ─────────────────────────────────────────────────────────────
# Scraper politeness / rate limiting
# ─────────────────────────────────────────────────────────────
REQUEST_DELAYS: Dict[str, Tuple[float, float]] = {
    "alibaba": (5.0, 12.0),
    "indiamart": (3.0, 8.0),
    "hktdc": (3.0, 7.0),
    "importyeti": (2.0, 5.0),
}

MAX_REQUESTS_PER_SESSION: Dict[str, int] = {
    "alibaba": 15,
    "indiamart": 25,
    "hktdc": 50,
}


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
LOG_LEVEL = (os.getenv("SUPPLIER_INTEL_LOG_LEVEL") or "INFO").upper()

_REDACT_PARAM_PATTERN = re.compile(r"(?i)\b(api_key|key|token)=([^&\s\"']*)")


class _RedactApiKeysFilter(logging.Filter):
    """Masks `api_key=`/`key=`/`token=` URL-parameter values
    (case-insensitive) in every log record reaching the handler this is
    attached to -- this codebase's scrapers/verifiers pass credentials
    as query-string parameters to Apify, SerpAPI, Google Places, Amap,
    and Qichacha, and any of those URLs reaching a log line would leak
    the key in plain text.

    Deliberately attached to the HANDLER (see `configure_logging`
    below), not to any one logger's own `.filters` list. Python's
    logging module only runs a logger's own filters against records
    that originate from a direct call on that specific logger object --
    propagating a record up the logger hierarchy does not re-check each
    ancestor logger's filters, only each Handler's, as the record
    actually reaches it (`Logger.callHandlers` walks the handler list,
    calling `handler.handle()`, which is what invokes the handler's own
    filters). `httpx` -- which every scraper's HTTP client is built on
    -- logs every outbound request URL at INFO through its own `httpx`
    child logger, not through this application's loggers, so a filter
    attached only to the root logger's own `.filters` list would never
    see those records at all; the handler is the one place every
    record, this application's own and every propagated third-party
    one, actually passes through.

    Redacts the fully-assembled message -- `record.getMessage()`, which
    is `str(record.msg) % record.args` -- rather than redacting
    `record.msg` and `record.args` separately. That would have a real
    bug: a message *template* can contain a literal `"key=%s"`
    substring, which is a placeholder, not a secret -- but the same
    regex that must catch a real secret embedded in an arg's string
    form would just as happily "redact" that placeholder text itself,
    corrupting the template before `%`-substitution ever runs (and
    then crashing formatting entirely, since the substituted arg no
    longer has a placeholder left to land in). Redacting only the
    final, already-substituted text sidesteps this. It also means no
    special-casing is needed for httpx passing its request URL as an
    `httpx.URL` object rather than a plain string: `%s` % that object
    already calls `str()` on it as part of ordinary `%`-formatting,
    the same way it would for any other non-string arg (e.g. `%d` with
    a real int) -- `record.getMessage()` does that stringification
    itself, correctly, for every arg type, before this filter ever
    needs to look at the text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _REDACT_PARAM_PATTERN.sub(r"\1=***REDACTED***", record.getMessage())
        record.args = None
        return True


def configure_logging() -> None:
    """Idempotent logging setup. Safe to call multiple times (e.g. once
    from main.py and once from a test fixture) without duplicating handlers."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redaction_filter = _RedactApiKeysFilter()
    for handler in root.handlers:
        handler.addFilter(redaction_filter)
