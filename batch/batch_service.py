"""
batch/batch_service.py

Orchestrates a CSV batch upload through the EXISTING single-company
enrichment path -- deduplication.matcher.SupplierMatcher.resolve_and_store()
+ collection.collection_service.CollectionService.collect() -- one call
per row, no second extraction pipeline. See batch/csv_parser.py for how
rows get here, and storage/database.py's batch_upload_rows/
field_provenance comments for the schema this writes to.

Row handling:
- company_name + website: the normal path, identical to how
  discovery/pipeline.orchestrator already resolve a fresh candidate --
  resolve_and_store() then collect().
- company_name, no website: "needs_url" -- never guessed or searched,
  passed through untouched. No enrichment call is ever made for these.
- website, no company_name: a placeholder name is derived from the
  domain (e.g. "acmetrailer.com" -> "Acmetrailer"), tagged
  name_source="inferred_from_domain". Dedup for these rows uses a
  DIRECT domain lookup (repo.find_by_domain) instead of
  SupplierMatcher.resolve_and_store() -- a synthetic, domain-derived
  name is far more collision-prone under fuzzy name matching than a
  real company name would be (two unrelated companies "acmetrailer.com"
  and "acme-trailer.net" both plausibly deriving something like "Acme
  Trailer"), and the existing domain-exact-match tier already gives the
  right merge/create answer without ever needing the synthetic name at
  all. After collect(), a grounded name-extraction pass -- literally
  the same SYSTEM_PROMPT discovery.candidate_validator.py already uses
  ("only report a name if explicitly stated in the text, never guess")
  -- attempts to replace the placeholder with a real one. Three
  outcomes (see _attempt_name_extraction's own docstring for the
  exact logic): applied (name passed a junk/parking-page floor test
  and the supplier still had the placeholder -- written to
  canonical_name + field_provenance), rejected (extraction found a
  server-default/parking-page name like "nginx" -- nothing written,
  reason recorded on the batch row), or conflicting (the supplier
  already has a trusted name from elsewhere -- never overwritten, but
  the disagreement is still recorded via field_provenance under
  field_name="canonical_name_candidate" as a signal, not applied).
- neither name nor website: needs_url (a row telling us nothing about
  the company is, at minimum, still missing a URL).
- website resolves to a bare marketplace root or any *.alibaba.com/
  made-in-china.com/tradeindia.com/etc. subdomain (deduplication.
  domain_utils.PLATFORM_REGISTERED_DOMAINS): "failed", hard, before any
  resolve/dedup/collect attempt -- a marketplace listing has no
  independently-verifiable company identity, and extract_domain()
  strips the URL's path/listing-ID entirely, so every row pointed at
  the same marketplace would otherwise collapse onto the exact same
  bare domain and merge via the ordinary domain-exact-match dedup tier
  below (found live: 11 distinct supplied names collapsed onto 2
  supplier records this way, each merge silently discarding a later
  row's real name and reporting "success").

Within-batch dedup: rows are resolved in order, and a domain already
resolved earlier in the SAME batch reuses that supplier_id rather than
re-running collect() -- avoids a redundant real headless-browser fetch
for an exact repeat (or near-repeat, e.g. "https://acmetrailer.com" vs
"acmetrailer.com/" vs "www.acmetrailer.com") within one upload. This is
a stronger, domain-normalised check than csv_parser.py's own
duplicate_row_indices (an exact string-pair match) -- that field is
left purely informational; multiple rows landing on the same
supplier_id in the export is itself the visible signal.

Address extraction (_attempt_address_extraction): runs for EVERY
successfully-collected row, not just placeholder-name rows -- address
isn't tied to whether the CSV gave a name. Candidate sources are tried
in a fixed order (contact page, then footer text, then impressum page,
then about/company page -- see _address_candidate_sources), stopping
at the first tier that yields an address; grounded-only prompt (ADDRESS_EXTRACTION_SYSTEM_PROMPT)
so a partial address (e.g. just a city) is stored as-is, never
completed. Same trusted-value guard as canonical_name: only written to
suppliers.address if currently empty, otherwise recorded via
field_provenance as field_name="address_candidate" -- a disagreement
signal, not applied.

Factory location extraction (_attempt_factory_location_extraction):
same tiered candidate sources, same grounded-only/trusted-value-guard
discipline as address extraction above -- but a deliberately SEPARATE
field, not a fallback for address. A supplier's registered/contact
address is frequently a different place than its actual production
site, so this asks specifically about the latter (square footage,
"our factory is located at X", explicit production-site mentions).

Facility photo aggregation (_attempt_facility_photo_aggregation): no
LLM call -- unions each collected page's facility_photo_urls (found at
collection time, see site_collector.py's _extract_facility_photo_urls)
into suppliers.candidate_facility_photo_urls. Unlike address/factory
location this always accumulates rather than gap-filling once: a
heuristic candidate photo list is evidence for the buyer's own manual
reverse-image-search review, never a verdict, so there's no
"conflict" to guard against -- more candidates found on a later run is
strictly more useful, never a correction to a wrong prior answer.

Reputation search (_attempt_reputation_search): OPT-IN (run_batch's
search_reputation flag, off by default -- real per-row SerpAPI cost,
3 searches/row). "[canonical_name] scam", "[canonical_name] review",
"[canonical_name] factory tour" via the same GoogleSearchScraper/
SerpAPI integration discovery already uses. Stores exactly the title/
link/snippet each query returned into supplier_reputation_snippets --
never a Clean/Flagged judgment of any kind; that call stays the
buyer's, made by reading the snippets themselves. Uses the supplier's
CURRENT canonical_name (i.e. after any name extraction earlier in this
same row) as the query subject -- a still-placeholder domain-derived
name would produce useless search results, so a supplier with no real
name yet is skipped for this row (there will be a next run).

Domain recovery (_attempt_domain_recovery): OPT-IN (run_batch's
recover_dead_domains flag, off by default -- real per-row SerpAPI+fetch+
LLM cost). Triggered only when this row's own collect() already failed
(a dead/unreachable domain on file). Reuses discovery.candidate_validator.
CandidateValidator.recover() -- the SAME real gate (real fetch, real LLM
name extraction, real product-term check, real trader check) any fresh
discovery candidate goes through; a recovered domain gets zero special
trust. Requires an explicit recovery_product_term (a CSV row has no
product concept of its own) -- run_batch fails closed (ValueError) if
recovery is enabled without one, rather than skipping or weakening the
product-term gate. On a successful recovery, the supplier's `domain`
field is overwritten with the recovered one -- safe here specifically
because it's this same row's own domain, just set moments earlier in
this same operation, not a pre-existing trusted value from elsewhere --
and collect() is re-attempted against the corrected domain.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from collection.collection_service import CollectionService
from batch.csv_parser import ParsedRow
from deduplication.domain_utils import extract_domain, is_platform_subdomain
from deduplication.matcher import SupplierMatcher
from discovery.candidate_validator import CandidateValidator
from discovery.candidate_validator import SYSTEM_PROMPT as NAME_EXTRACTION_SYSTEM_PROMPT
from llm.client import LLMClient
from scrapers.google_search_scraper import GoogleSearchScraper
from storage.repository import SupplierRepository
from verification.website_contact_extractor import parking_page_reason

logger = logging.getLogger(__name__)

# Whole-name (not substring) match -- a real company whose extracted
# name happens to CONTAIN "welcome to" (e.g. "Welcome to Chiming") must
# never be rejected on that basis; only an exact match against a known
# server-default/parking-page name is high-confidence enough to reject
# outright. Ambiguous cases are caught by parking_page_reason (see
# verification.website_contact_extractor -- shared across name, address,
# and contact extraction, not just this blocklist) instead of by
# growing this list.
_JUNK_NAME_EXACT_BLOCKLIST: frozenset = frozenset({
    "nginx", "welcome to nginx", "welcome to nginx!",
    "apache2 ubuntu default page", "apache2 debian default page",
    "apache http server test page", "it works", "it works!",
    "iis windows server", "welcome to iis", "internet information services",
    "index of", "index of /", "untitled", "untitled document", "untitled page",
    "coming soon", "under construction", "this site is under construction",
    "parked domain", "domain parked", "this domain is parked",
    "domain for sale", "this domain is for sale", "buy this domain",
    "default web site page", "test page", "cloudflare",
})


# A page with less real content than this (after stripping whitespace)
# isn't trustworthy enough for a grounded LLM CLAIM (a name or an
# address) regardless of what was extracted from it. NOT part of
# parking_page_reason itself -- that's shared with contact extraction
# now too, and a real "Contact Us" page is often legitimately this
# short (just an address/phone block), unlike a page an LLM is being
# asked to assert a company name or address from.
_MIN_MEANINGFUL_PAGE_TEXT_LENGTH = 60


def _reject_reason_for_llm_extraction(page_text: str) -> Optional[str]:
    """None if `page_text` is trustworthy enough to run a grounded LLM
    extraction (name or address) against; otherwise a reason. Layers
    this module's own length floor on top of the shared, signature-only
    parking_page_reason -- see that function's own docstring and
    _MIN_MEANINGFUL_PAGE_TEXT_LENGTH above for why the floor lives here
    rather than in the shared function."""
    reason = parking_page_reason(page_text)
    if reason:
        return reason
    if len(re.sub(r"\s+", "", page_text or "")) < _MIN_MEANINGFUL_PAGE_TEXT_LENGTH:
        return "page text is too short to be a real company page"
    return None


def _reject_reason_for_extracted_name(name: str, page_text: str) -> Optional[str]:
    """None if `name` passes the floor test; otherwise a human-readable
    rejection reason. See _JUNK_NAME_EXACT_BLOCKLIST for what's checked
    and why the name check is whole-string, not substring."""
    normalised_name = " ".join(name.strip().lower().rstrip("!.").split())
    if normalised_name in _JUNK_NAME_EXACT_BLOCKLIST:
        return f"extracted name '{name}' matches a known server-default/placeholder page name"

    return _reject_reason_for_llm_extraction(page_text)


# Grounded-only, same discipline as NAME_EXTRACTION_SYSTEM_PROMPT --
# critically, rule 2 is what makes "store the city, leave the rest
# empty" the default behaviour rather than something callers have to
# special-case: the model is told to return exactly the substring
# found, never to complete a partial address.
ADDRESS_EXTRACTION_SYSTEM_PROMPT = """You are reading the text of a company website page. Extract ONLY the company's own postal address if it is explicitly stated in the text below -- never guess, infer, or complete a partial address.

Rules, strictly enforced:
1. Only report an address if it is explicitly stated in the text (e.g. in a contact section, footer, or legal/impressum notice).
2. Return exactly what is stated -- do not add a street, postcode, city, or country that isn't present. If only a city or a partial address is given, return just that partial text -- never complete it using typical address patterns or general knowledge.
3. If no address is stated at all, return null.
4. Never invent or infer an address from a domain name, company name, or general knowledge about the company.

Return ONLY a JSON object with exactly this key, no other text:
{
  "address": "the exact address text as stated, or null if not clearly stated"
}"""

# Same grounded discipline as ADDRESS_EXTRACTION_SYSTEM_PROMPT, but
# deliberately asks about the factory/production site specifically --
# never treats a general contact/office address as the factory unless
# the text itself says so (see module docstring).
FACTORY_LOCATION_EXTRACTION_SYSTEM_PROMPT = """You are reading the text of a company website page. Extract ONLY the location of the company's actual factory/production site if it is explicitly stated in the text below -- never guess, infer, or complete a partial location, and never assume the factory is at the same address as a general contact/office address unless the text explicitly says so.

Look for: an explicit statement of where manufacturing/production takes place (e.g. "our factory is located in X", "production facility in Y"), a stated facility size (e.g. "50,000 sq ft manufacturing facility in X"), or an address specifically labelled as the factory/plant/production site -- as distinct from a general company or sales-office address.

Rules, strictly enforced:
1. Only report a factory/production location if it is explicitly stated in the text.
2. Return exactly what is stated -- do not add a street, postcode, city, or country that isn't present. If only a city or a partial location is given, return just that partial text -- never complete it using typical address patterns or general knowledge.
3. If no factory/production location is stated at all, return null.
4. Never invent or infer a factory location from a domain name, company name, general company address, or general knowledge about the company.

Return ONLY a JSON object with exactly this key, no other text:
{
  "factory_location": "the exact factory/production location text as stated, or null if not clearly stated"
}"""

# Cap on how many candidate facility photo URLs accumulate per supplier
# across repeated batch runs -- keeps the export usable; a buyer
# reviewing 10 candidate photos by hand is reasonable, 200 is not.
_MAX_FACILITY_PHOTOS_PER_SUPPLIER = 10

# Criterion-D query subjects (query_type -> the literal suffix appended
# to the company name) -- see _attempt_reputation_search. query_type
# values match supplier_reputation_snippets' CHECK constraint exactly.
_REPUTATION_QUERY_SUFFIXES: Dict[str, str] = {
    "scam": "scam",
    "review": "review",
    "factory_tour": "factory tour",
}
_MAX_REPUTATION_SNIPPETS_PER_QUERY = 5


@dataclass
class BatchOutcome:
    total_rows: int = 0
    needs_url: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    placeholder_names_used: int = 0
    placeholder_names_replaced: int = 0
    placeholder_names_rejected: int = 0
    placeholder_names_conflicting: int = 0
    addresses_found: int = 0
    addresses_conflicting: int = 0
    factory_locations_found: int = 0
    factory_locations_conflicting: int = 0
    facility_photos_found: int = 0
    reputation_snippets_found: int = 0
    domains_recovered: int = 0


def _address_candidate_sources(pages: List[Any]) -> List[tuple]:
    """Ordered (tier_label, url, text) candidates for address
    extraction -- contact page, footer text, impressum page, about
    page, per the required preference order. Only the first page found
    in each tier is used (at most one candidate per tier, so at most 4
    LLM calls total per row -- see _attempt_address_extraction, which
    stops at the first tier that actually yields an address).

    "about page" (added after a real gap-analysis run against the 29
    confirmed injection-moulding candidates -- see batch_service's own
    diagnostic notes): a general company-info page is the LEAST
    authoritative of the four tiers (a dedicated contact/impressum page
    states an address on purpose; an about page mentions one
    incidentally, if at all) -- deliberately tried last, only once
    contact/footer/impressum have all come up empty. Matches "about" OR
    "company" in the URL -- real sites use both conventions for the
    same page (e.g. plasticmold.net/company/, hordrt.com/about-us-3/)."""
    candidates: List[tuple] = []

    contact_page = next(
        (p for p in pages if "contact" in (getattr(p, "url", "") or "").lower()
         and (getattr(p, "text", "") or "").strip()),
        None,
    )
    if contact_page is not None:
        candidates.append(("contact page", contact_page.url, contact_page.text))

    footer_page = next((p for p in pages if (getattr(p, "footer_text", "") or "").strip()), None)
    if footer_page is not None:
        candidates.append(("footer", footer_page.url, footer_page.footer_text))

    impressum_page = next(
        (p for p in pages
         if any(k in (getattr(p, "url", "") or "").lower() for k in ("impressum", "imprint"))
         and (getattr(p, "text", "") or "").strip()),
        None,
    )
    if impressum_page is not None:
        candidates.append(("impressum page", impressum_page.url, impressum_page.text))

    about_page = next(
        (p for p in pages
         if any(k in (getattr(p, "url", "") or "").lower() for k in ("about", "company"))
         and (getattr(p, "text", "") or "").strip()),
        None,
    )
    if about_page is not None:
        candidates.append(("about page", about_page.url, about_page.text))

    return candidates


def _placeholder_name_from_domain(domain: str) -> str:
    """A readable placeholder, not a guess at the real name -- e.g.
    "acmetrailer.com" -> "Acmetrailer". Deliberately dumb (no attempt at
    word-splitting "acmetrailer" into "Acme Trailer") -- inventing word
    boundaries would look like a real extracted name, which this
    explicitly is not; name_source="inferred_from_domain" is what marks
    it as synthetic. This just needs to produce *something* non-empty
    for create_golden_record's canonical_name requirement."""
    registrable = (domain or "").split(".")[0]
    cleaned = registrable.replace("-", " ").replace("_", " ").strip()
    return cleaned.title() if cleaned else domain


class BatchService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        matcher: Optional[SupplierMatcher] = None,
        collection_service: Optional[CollectionService] = None,
        llm_client: Optional[LLMClient] = None,
        google_scraper: Optional[GoogleSearchScraper] = None,
        candidate_validator: Optional[CandidateValidator] = None,
        default_region_fallback: Optional[str] = None,
    ):
        self.repo = repo or SupplierRepository()
        self.matcher = matcher or SupplierMatcher(self.repo)
        # default_region_fallback only applies when this class constructs
        # its own CollectionService -- an explicitly-injected one (tests,
        # or a caller with its own configured instance) is used exactly
        # as given, same precedent as candidate_validator below. See
        # main.py collect --default-region's own help text for what this
        # is for: phonenumbers can't parse a national-format number (no
        # +44 prefix) without a region hint, and collect() always runs
        # BEFORE address/country extraction in this class's own row loop,
        # so a freshly-created supplier's country is genuinely unknown at
        # the exact moment contact extraction runs. main.py batch-upload
        # had no equivalent to main.py collect's own --default-region at
        # all until now -- found live: re-collecting a real 71-row batch
        # with a GB fallback recovered 41 more phone numbers than without
        # one, dwarfing every other phone-extraction gap combined.
        self.collection_service = collection_service or CollectionService(
            repo=self.repo, default_region_fallback=default_region_fallback,
        )
        self.llm_client = llm_client or LLMClient()
        self.google_scraper = google_scraper or GoogleSearchScraper()
        if candidate_validator is not None:
            self.candidate_validator = candidate_validator
        else:
            # Lazy, only-constructed-if-needed -- same OwnWebsiteScraper-backed
            # convention discovery_service.py already uses for the identical
            # purpose (CandidateValidator needs a lightweight .fetch(domain)
            # object, not the heavier Playwright-based SiteCollector this
            # class otherwise uses for a row's real collection).
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.candidate_validator = CandidateValidator(
                website_fetcher=OwnWebsiteScraper(), llm_client=self.llm_client,
            )

    def run_batch(
        self, rows: List[ParsedRow], batch_job_id: str,
        progress_callback: Optional[Callable[[BatchOutcome], None]] = None,
        search_reputation: bool = False,
        recover_dead_domains: bool = False,
        recovery_product_term: Optional[str] = None,
    ) -> BatchOutcome:
        if recover_dead_domains and not recovery_product_term:
            raise ValueError(
                "recovery_product_term is required when recover_dead_domains=True -- "
                "candidate_validator's product-term gate has no CSV-row product concept "
                "to fall back on, and this must fail closed rather than skip that gate."
            )
        outcome = BatchOutcome(total_rows=len(rows))
        # domain -> already-resolved supplier_id within THIS batch call --
        # see module docstring's "within-batch dedup" section.
        resolved_domains: Dict[str, int] = {}
        # domain -> terminal {"status", "error_message"} from the FIRST row
        # that processed this domain in this batch -- a repeat reuses this
        # instead of calling collect() again (see is_repeat below).
        domain_outcomes: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            batch_row_id = self.repo.create_batch_upload_row(
                batch_job_id=batch_job_id, row_index=row.row_index,
                original_columns=row.original_columns,
                company_name=row.company_name, website=row.website,
            )

            if not row.website:
                outcome.needs_url += 1
                self.repo.update_batch_upload_row(batch_row_id, {"status": "needs_url"})
                self._report(progress_callback, outcome)
                continue

            domain = extract_domain(row.website)
            if not domain:
                outcome.needs_url += 1
                self.repo.update_batch_upload_row(batch_row_id, {
                    "status": "needs_url", "error_message": "website did not parse to a usable domain",
                })
                self._report(progress_callback, outcome)
                continue

            if is_platform_subdomain(domain):
                # A marketplace ROOT (or any *.alibaba.com/etc. subdomain --
                # see is_platform_subdomain's own docstring) has no company-
                # specific identity to verify against: extract_domain() also
                # strips the URL's path/listing-ID entirely, so every row
                # pointed at the same marketplace collapses onto the exact
                # same bare domain -- found live: 11 distinct supplied
                # company names on made-in-china.com/alibaba.com/
                # tradeindia.com collapsed onto just 2 supplier records via
                # the ordinary domain-exact-match dedup tier below, each
                # merge silently discarding the later row's own real name
                # and reporting "success" as if it verified that company.
                # Hard-fail here, before any resolve/dedup/collect
                # attempt -- discovery's own candidate_validator.validate()
                # already gates this the same way (gate 2); batch upload's
                # plain company_name+website path had no equivalent check.
                outcome.failed += 1
                self.repo.update_batch_upload_row(batch_row_id, {
                    "status": "failed",
                    "error_message": f"marketplace root URL ({domain}) -- not a specific listing, cannot verify",
                })
                self._report(progress_callback, outcome)
                continue

            is_repeat = domain in resolved_domains
            try:
                if row.company_name:
                    supplier_id = self._resolve_named_row(row.company_name, domain, resolved_domains)
                    name_source = "csv"
                else:
                    supplier_id = self._resolve_placeholder_row(domain, resolved_domains)
                    name_source = "inferred_from_domain"
                    outcome.placeholder_names_used += 1
            except Exception as e:  # noqa: BLE001 -- one row's failure must never abort the whole batch
                logger.error("batch: resolve failed for row %s (%r): %s", row.row_index, row.website, e)
                outcome.failed += 1
                self.repo.update_batch_upload_row(batch_row_id, {"status": "failed", "error_message": str(e)})
                self._report(progress_callback, outcome)
                continue

            self.repo.update_batch_upload_row(batch_row_id, {
                "status": "processing", "supplier_id": supplier_id, "name_source": name_source,
            })
            outcome.processed += 1

            if is_repeat:
                # Same domain already collected earlier in this batch --
                # reuse that outcome instead of a second real
                # headless-browser fetch (see module docstring).
                cached = domain_outcomes[domain]
                if cached["status"] == "success":
                    outcome.succeeded += 1
                    self.repo.update_batch_upload_row(batch_row_id, {"status": "success"})
                else:
                    outcome.failed += 1
                    self.repo.update_batch_upload_row(batch_row_id, {
                        "status": "failed", "error_message": cached.get("error_message"),
                    })
                self._report(progress_callback, outcome)
                continue

            try:
                # return_pages=True unconditionally now -- address
                # extraction (below) runs for every row, not just
                # placeholder-name rows.
                collect_result = self.collection_service.collect(
                    supplier_id, return_pages=True, source_url=row.website,
                )
            except Exception as e:  # noqa: BLE001 -- CollectionService.collect() already never raises;
                # this is defence in depth, matching every other stage's per-row fault isolation.
                logger.error("batch: collect failed for supplier #%s: %s", supplier_id, e)
                outcome.failed += 1
                self.repo.update_batch_upload_row(batch_row_id, {"status": "failed", "error_message": str(e)})
                domain_outcomes[domain] = {"status": "failed", "error_message": str(e)}
                self._report(progress_callback, outcome)
                continue

            if recover_dead_domains and collect_result.get("status") != "success":
                recovered_result = self._attempt_domain_recovery(
                    supplier_id, row, domain, recovery_product_term,
                )
                if recovered_result is not None:
                    outcome.domains_recovered += 1
                    collect_result = recovered_result

            if name_source == "inferred_from_domain":
                extraction_outcome = self._attempt_name_extraction(supplier_id, collect_result, batch_row_id)
                if extraction_outcome == "applied":
                    outcome.placeholder_names_replaced += 1
                elif extraction_outcome == "rejected":
                    outcome.placeholder_names_rejected += 1
                elif extraction_outcome == "conflicting":
                    outcome.placeholder_names_conflicting += 1

            address_outcome = self._attempt_address_extraction(supplier_id, collect_result)
            if address_outcome == "applied":
                outcome.addresses_found += 1
            elif address_outcome == "conflicting":
                outcome.addresses_conflicting += 1

            factory_location_outcome = self._attempt_factory_location_extraction(supplier_id, collect_result)
            if factory_location_outcome == "applied":
                outcome.factory_locations_found += 1
            elif factory_location_outcome == "conflicting":
                outcome.factory_locations_conflicting += 1

            if self._attempt_facility_photo_aggregation(supplier_id, collect_result):
                outcome.facility_photos_found += 1

            if search_reputation:
                snippets_saved = self._attempt_reputation_search(supplier_id)
                if snippets_saved:
                    outcome.reputation_snippets_found += 1

            if collect_result.get("status") == "success":
                outcome.succeeded += 1
                self.repo.update_batch_upload_row(batch_row_id, {"status": "success"})
                domain_outcomes[domain] = {"status": "success"}
            else:
                error_message = collect_result.get("error")
                outcome.failed += 1
                self.repo.update_batch_upload_row(batch_row_id, {
                    "status": "failed", "error_message": error_message,
                })
                domain_outcomes[domain] = {"status": "failed", "error_message": error_message}

            self._report(progress_callback, outcome)

        return outcome

    @staticmethod
    def _report(progress_callback: Optional[Callable[[BatchOutcome], None]], outcome: BatchOutcome) -> None:
        if progress_callback:
            try:
                progress_callback(outcome)
            except Exception as e:  # noqa: BLE001 -- a progress-reporting glitch must never abort the batch
                logger.warning("batch: progress callback failed: %s", e)

    def _resolve_named_row(self, company_name: str, domain: str, resolved_domains: Dict[str, int]) -> int:
        if domain in resolved_domains:
            return resolved_domains[domain]
        candidate = {"canonical_name": company_name, "domain": domain}
        result = self.matcher.resolve_and_store(candidate)
        supplier_id = result.get("supplier_id") or result.get("new_supplier_id")
        resolved_domains[domain] = supplier_id
        return supplier_id

    def _resolve_placeholder_row(self, domain: str, resolved_domains: Dict[str, int]) -> int:
        """Direct domain lookup, never SupplierMatcher's fuzzy name
        matching -- see module docstring for why."""
        if domain in resolved_domains:
            return resolved_domains[domain]
        placeholder_name = _placeholder_name_from_domain(domain)
        existing = self.repo.find_by_domain(domain)
        if existing:
            self.repo.merge_into_golden(existing["id"], {"canonical_name": placeholder_name, "domain": domain})
            supplier_id = existing["id"]
        else:
            supplier_id = self.repo.create_golden_record({"canonical_name": placeholder_name, "domain": domain})
        resolved_domains[domain] = supplier_id
        return supplier_id

    def _attempt_name_extraction(self, supplier_id: int, collect_result: Dict[str, Any], batch_row_id: int) -> str:
        """Reuses discovery.candidate_validator.SYSTEM_PROMPT verbatim --
        same grounded-only extraction discipline, applied here to a
        page already fetched for collection rather than a fresh one.

        Returns one of:
        - "skipped"     -- no pages / no text / LLM call failed / no name found
        - "rejected"    -- a name WAS extracted but failed the junk/parking-page
                           floor test (_reject_reason_for_extracted_name) --
                           found via a real calibration run: a bare nginx
                           landing page confidently "extracted" the company
                           name "nginx" and it was written straight to the
                           golden record. Nothing is written; the reason is
                           recorded on the batch row.
        - "conflicting" -- the name passed the floor test, but the supplier's
                           canonical_name is no longer the domain-derived
                           placeholder (some trusted source -- e.g. a bulk
                           import -- already named it) -- never overwritten
                           by a lower-confidence guess. The extracted value
                           is still recorded via field_provenance, under
                           field_name="canonical_name_candidate" (never
                           "canonical_name", so it's never mistaken for the
                           applied value) -- a disagreement between the
                           trusted name and the site's own content is a real
                           signal about the supplier, not noise to discard.
        - "applied"     -- the name passed every check and was written to
                           BOTH the supplier record (canonical_name, the
                           authoritative value) AND this batch row's own
                           company_name -- the row-level copy exists so a
                           row that's later re-queried in isolation (e.g. by
                           the export or a review view) doesn't have to join
                           back to suppliers just to see what was actually
                           found; csv_exporter.py still prefers the live
                           supplier value as the ultimate source of truth,
                           this is a consistency nicety, not the only place
                           the real value lives.
        """
        pages = collect_result.get("pages") or []
        if not pages:
            return "skipped"
        page = pages[0]
        page_text = getattr(page, "text", "") or ""
        if not page_text.strip():
            return "skipped"

        try:
            extracted = self.llm_client.complete_json(
                NAME_EXTRACTION_SYSTEM_PROMPT, f"Website page content:\n\n{page_text[:20_000]}",
            )
        except Exception as e:  # noqa: BLE001 -- an extraction failure must never fail an otherwise-successful collection
            logger.warning("batch: name extraction failed for supplier #%s: %s", supplier_id, e)
            return "skipped"
        if not isinstance(extracted, dict):
            return "skipped"

        name = extracted.get("company_name")
        if not isinstance(name, str) or not name.strip():
            return "skipped"
        name = name.strip()
        page_url = getattr(page, "url", None)

        reject_reason = _reject_reason_for_extracted_name(name, page_text)
        if reject_reason:
            logger.info(
                "batch: rejected extracted name '%s' for supplier #%s (%s)", name, supplier_id, reject_reason,
            )
            self.repo.update_batch_upload_row(batch_row_id, {
                "name_extraction_note": f"rejected: {reject_reason}",
            })
            return "rejected"

        supplier = self.repo.get_supplier(supplier_id)
        current_name = (supplier or {}).get("canonical_name")
        expected_placeholder = _placeholder_name_from_domain((supplier or {}).get("domain") or "")

        if current_name is not None and current_name != expected_placeholder:
            # A trusted source already named this supplier -- never let a
            # lower-confidence guess overwrite it, but still surface the
            # disagreement (see this method's own docstring).
            logger.info(
                "batch: extracted name '%s' conflicts with existing trusted name '%s' for "
                "supplier #%s -- not applied", name, current_name, supplier_id,
            )
            self.repo.save_field_provenance(
                supplier_id=supplier_id, field_name="canonical_name_candidate", value=name,
                source_url=page_url, raw_snippet=page_text[:500],
                extraction_method="llm_grounded_extraction",
                source_tier="own_domain", claim_type="verifiable_fact",
            )
            self.repo.update_batch_upload_row(batch_row_id, {
                "name_extraction_note": (
                    f"skipped: supplier already named '{current_name}'; "
                    f"site extraction found '{name}' instead -- see field_provenance"
                ),
            })
            return "conflicting"

        self.repo.update_supplier_fields_with_history(
            supplier_id, {"canonical_name": name},
            changed_by="batch_service",
            change_reason="replaced domain-derived placeholder with a name explicitly found on the supplier's own site",
        )
        self.repo.save_field_provenance(
            supplier_id=supplier_id, field_name="canonical_name", value=name,
            source_url=page_url, raw_snippet=page_text[:500],
            extraction_method="llm_grounded_extraction",
            source_tier="own_domain", claim_type="verifiable_fact",
        )
        self.repo.update_batch_upload_row(batch_row_id, {"company_name": name})
        return "applied"

    def _attempt_address_extraction(self, supplier_id: int, collect_result: Dict[str, Any]) -> str:
        """Runs for EVERY successfully-collected row, regardless of
        name_source -- address isn't tied to whether the CSV gave a
        name. Tries candidate sources in the order specified: contact
        page, then footer text, then impressum page -- only the first
        page found in each tier, stopping at the first tier that
        actually yields an address (never blending across tiers, never
        making more than one LLM call per tier).

        Returns "applied" (address was empty, now written to
        suppliers.address + field_provenance), "conflicting" (supplier
        already had a non-empty address from elsewhere -- never
        overwritten, but the extracted value is still recorded via
        field_provenance under field_name="address_candidate" so a
        disagreement between the trusted address and the site's own
        content is visible, same pattern as canonical_name_candidate),
        or "skipped" (no pages, every tier empty/parking-page-shaped,
        or no address found anywhere)."""
        pages = collect_result.get("pages") or []
        if not pages:
            return "skipped"

        for tier_label, url, text in _address_candidate_sources(pages):
            if _reject_reason_for_llm_extraction(text):
                continue
            try:
                extracted = self.llm_client.complete_json(
                    ADDRESS_EXTRACTION_SYSTEM_PROMPT,
                    f"Website page content ({tier_label}):\n\n{text[:20_000]}",
                )
            except Exception as e:  # noqa: BLE001 -- an extraction failure must never fail an otherwise-successful collection
                logger.warning("batch: address extraction failed for supplier #%s (%s): %s", supplier_id, tier_label, e)
                continue
            if not isinstance(extracted, dict):
                continue

            address = extracted.get("address")
            if not isinstance(address, str) or not address.strip():
                continue
            address = address.strip()

            supplier = self.repo.get_supplier(supplier_id)
            current_address = (supplier or {}).get("address")

            if current_address:
                logger.info(
                    "batch: extracted address for supplier #%s (%s) conflicts with existing "
                    "address -- not applied", supplier_id, tier_label,
                )
                self.repo.save_field_provenance(
                    supplier_id=supplier_id, field_name="address_candidate", value=address,
                    source_url=url, raw_snippet=text[:500],
                    extraction_method="llm_grounded_extraction",
                    source_tier="own_domain", claim_type="verifiable_fact",
                )
                return "conflicting"

            self.repo.update_supplier_fields_with_history(
                supplier_id, {"address": address},
                changed_by="batch_service",
                change_reason=f"address found on the supplier's own site ({tier_label})",
            )
            self.repo.save_field_provenance(
                supplier_id=supplier_id, field_name="address", value=address,
                source_url=url, raw_snippet=text[:500],
                extraction_method="llm_grounded_extraction",
                source_tier="own_domain", claim_type="verifiable_fact",
            )
            return "applied"

        return "skipped"

    def _attempt_factory_location_extraction(self, supplier_id: int, collect_result: Dict[str, Any]) -> str:
        """Same tiered-candidate/trusted-value-guard pattern as
        _attempt_address_extraction immediately above, reusing the exact
        same candidate pages (contact page, then footer text, then
        impressum page, then about/company page) -- deliberately a
        SEPARATE field from address,
        not a fallback for it: a supplier's registered/contact address
        is frequently a different place than its actual production
        site, and FACTORY_LOCATION_EXTRACTION_SYSTEM_PROMPT asks
        specifically about the latter (square footage, "our factory is
        located at X", explicit production-site mentions) -- never
        inferred from a general company address.

        Returns "applied" (factory_location was empty, now written to
        suppliers.factory_location + field_provenance), "conflicting"
        (supplier already had a non-empty factory_location from
        elsewhere -- never overwritten, extracted value recorded via
        field_provenance under field_name="factory_location_candidate",
        same pattern as address_candidate), or "skipped" (no pages,
        every tier empty/parking-page-shaped, or no factory location
        found anywhere)."""
        pages = collect_result.get("pages") or []
        if not pages:
            return "skipped"

        for tier_label, url, text in _address_candidate_sources(pages):
            if _reject_reason_for_llm_extraction(text):
                continue
            try:
                extracted = self.llm_client.complete_json(
                    FACTORY_LOCATION_EXTRACTION_SYSTEM_PROMPT,
                    f"Website page content ({tier_label}):\n\n{text[:20_000]}",
                )
            except Exception as e:  # noqa: BLE001 -- an extraction failure must never fail an otherwise-successful collection
                logger.warning(
                    "batch: factory location extraction failed for supplier #%s (%s): %s",
                    supplier_id, tier_label, e,
                )
                continue
            if not isinstance(extracted, dict):
                continue

            factory_location = extracted.get("factory_location")
            if not isinstance(factory_location, str) or not factory_location.strip():
                continue
            factory_location = factory_location.strip()

            supplier = self.repo.get_supplier(supplier_id)
            current_factory_location = (supplier or {}).get("factory_location")

            if current_factory_location:
                logger.info(
                    "batch: extracted factory location for supplier #%s (%s) conflicts with existing "
                    "factory_location -- not applied", supplier_id, tier_label,
                )
                self.repo.save_field_provenance(
                    supplier_id=supplier_id, field_name="factory_location_candidate", value=factory_location,
                    source_url=url, raw_snippet=text[:500],
                    extraction_method="llm_grounded_extraction",
                    source_tier="own_domain", claim_type="verifiable_fact",
                )
                return "conflicting"

            self.repo.update_supplier_fields_with_history(
                supplier_id, {"factory_location": factory_location},
                changed_by="batch_service",
                change_reason=f"factory location found on the supplier's own site ({tier_label})",
            )
            self.repo.save_field_provenance(
                supplier_id=supplier_id, field_name="factory_location", value=factory_location,
                source_url=url, raw_snippet=text[:500],
                extraction_method="llm_grounded_extraction",
                source_tier="own_domain", claim_type="verifiable_fact",
            )
            return "applied"

        return "skipped"

    def _attempt_facility_photo_aggregation(self, supplier_id: int, collect_result: Dict[str, Any]) -> int:
        """No LLM call -- unions every collected page's
        facility_photo_urls (computed at collection time, see
        site_collector.py's _extract_facility_photo_urls) into
        suppliers.candidate_facility_photo_urls. Always accumulates
        rather than gap-filling once (see module docstring): a repeat
        collection run might turn up different photos on a changed
        site, and dropping a previously-found candidate would be a
        real loss for something the buyer is meant to click through by
        hand later. Capped at _MAX_FACILITY_PHOTOS_PER_SUPPLIER total.

        Returns how many NEW urls were added this call (0 if none)."""
        pages = collect_result.get("pages") or []
        if not pages:
            return 0

        found_urls: List[str] = []
        for page in pages:
            for url in getattr(page, "facility_photo_urls", None) or []:
                if url not in found_urls:
                    found_urls.append(url)
        if not found_urls:
            return 0

        supplier = self.repo.get_supplier(supplier_id)
        combined = list((supplier or {}).get("candidate_facility_photo_urls") or [])
        added = 0
        for url in found_urls:
            if url not in combined:
                combined.append(url)
                added += 1
        if not added:
            return 0

        self.repo.update_supplier_fields_with_history(
            supplier_id, {"candidate_facility_photo_urls": combined[:_MAX_FACILITY_PHOTOS_PER_SUPPLIER]},
            changed_by="batch_service",
            change_reason="candidate facility photo URLs found during collection",
        )
        return added

    def _attempt_domain_recovery(
        self, supplier_id: int, row: ParsedRow, domain: str, recovery_product_term: str,
    ) -> Optional[Dict[str, Any]]:
        """OPT-IN (see run_batch's recover_dead_domains flag), called only
        after this row's own collect() already failed against `domain`.
        Reuses discovery.candidate_validator.CandidateValidator.recover() --
        the SAME real gate (real fetch, real LLM name extraction, real
        product-term check, real trader check) any fresh discovery candidate
        goes through, capped at 2 search results, stopping at the first that
        validates. A recovered candidate gets zero special trust: nothing
        here skips or weakens that gate.

        Uses row.company_name if the CSV gave one, else the same domain-
        derived placeholder name already computed for this row
        (_placeholder_name_from_domain) -- not usable as a stored
        canonical_name (see _attempt_reputation_search's own
        expected_placeholder check), but perfectly usable as a search query.

        Passes the supplier's own on-file country as recover()'s
        existing_country hint, when set -- an extra, independent
        corroboration signal recover() checks a recovered candidate
        against (see that method's own docstring for why this exists:
        a real pilot run false-matched a dead domain onto an unrelated,
        similarly-named company; country corroboration catches a
        different flavour of that same risk).

        On a successful recovery: overwrites the supplier's `domain` field
        with the recovered one -- safe here specifically because it's this
        same row's own domain, set moments earlier in this very call, not a
        pre-existing trusted value from elsewhere (the trusted-value guard
        protects values set by a PRIOR run/source, not one this same
        operation just created) -- then re-attempts collect() against the
        corrected domain and returns that outcome.

        Returns None (leaving the row's original failed collect_result as
        the final outcome) if nothing recovered, the search itself errored,
        or every candidate failed the gate."""
        company_name = row.company_name or _placeholder_name_from_domain(domain)
        supplier = self.repo.get_supplier(supplier_id)
        existing_country = (supplier or {}).get("country")
        try:
            recovery = self.candidate_validator.recover(
                company_name, recovery_product_term, self.google_scraper,
                existing_country=existing_country,
            )
        except Exception as e:  # noqa: BLE001 -- one row's recovery failure must never abort the batch
            logger.error("batch: domain recovery failed for supplier #%s (%r): %s", supplier_id, company_name, e)
            return None
        if recovery is None:
            return None

        recovered_domain = recovery.candidate.domain
        self.repo.update_supplier_fields_with_history(
            supplier_id, {"domain": recovered_domain}, changed_by="batch_service",
            change_reason=f"domain recovery: {domain!r} unreachable, recovered as {recovered_domain!r}",
        )
        return self.collection_service.collect(supplier_id, return_pages=True, source_url=recovered_domain)

    def _attempt_reputation_search(self, supplier_id: int) -> int:
        """OPT-IN (see run_batch's search_reputation flag) criterion-D
        evidence gathering: three real SerpAPI searches per call --
        "[name] scam", "[name] review", "[name] factory tour" -- via the
        same GoogleSearchScraper integration discovery already uses.
        Stores exactly the title/link/snippet each query returned into
        supplier_reputation_snippets; makes NO Clean/Flagged judgment of
        any kind -- that call stays the buyer's, made by reading the
        snippets themselves.

        Uses the supplier's CURRENT canonical_name (i.e. after any name
        extraction earlier in this same row) as the query subject -- a
        still-placeholder domain-derived name (see
        _placeholder_name_from_domain) would produce useless search
        results, so a supplier with no real name yet is skipped (there
        will be a next run once a name is found).

        Returns how many NEW snippets were saved this call (0 if no
        usable name, every query errored, or every query returned
        nothing).

        Marks reputation_search_attempted_at on every call regardless of
        outcome (same discipline as capability_extracted_at/
        catalogue_depth_extracted_at) -- so the Audit tab can tell
        "searched, found nothing" apart from "never run"."""
        self.repo.mark_reputation_search_attempted(supplier_id)
        supplier = self.repo.get_supplier(supplier_id)
        company_name = (supplier or {}).get("canonical_name")
        if not company_name:
            return 0
        expected_placeholder = _placeholder_name_from_domain((supplier or {}).get("domain") or "")
        if company_name == expected_placeholder:
            return 0

        snippets: List[Dict[str, Any]] = []
        for query_type, suffix in _REPUTATION_QUERY_SUFFIXES.items():
            query_text = f"{company_name} {suffix}"
            try:
                results = self.google_scraper.scrape(query_text, max_results=_MAX_REPUTATION_SNIPPETS_PER_QUERY)
            except Exception as e:  # noqa: BLE001 -- one query failing must never abort the batch row
                logger.warning(
                    "batch: reputation search failed for supplier #%s (%s): %s", supplier_id, query_type, e,
                )
                continue
            for result in results[:_MAX_REPUTATION_SNIPPETS_PER_QUERY]:
                if not result.success:
                    continue
                raw = result.raw_data or {}
                snippets.append({
                    "query_type": query_type, "query_text": query_text,
                    "title": raw.get("title"), "link": raw.get("link"), "snippet": raw.get("snippet"),
                })

        if not snippets:
            return 0
        return self.repo.save_reputation_snippets(supplier_id, snippets)
