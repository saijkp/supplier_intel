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
in a fixed order (contact page, then footer text, then impressum page
-- see _address_candidate_sources), stopping at the first tier that
yields an address; grounded-only prompt (ADDRESS_EXTRACTION_SYSTEM_PROMPT)
so a partial address (e.g. just a city) is stored as-is, never
completed. Same trusted-value guard as canonical_name: only written to
suppliers.address if currently empty, otherwise recorded via
field_provenance as field_name="address_candidate" -- a disagreement
signal, not applied.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from collection.collection_service import CollectionService
from batch.csv_parser import ParsedRow
from deduplication.domain_utils import extract_domain
from deduplication.matcher import SupplierMatcher
from discovery.candidate_validator import SYSTEM_PROMPT as NAME_EXTRACTION_SYSTEM_PROMPT
from llm.client import LLMClient
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)

# Whole-name (not substring) match -- a real company whose extracted
# name happens to CONTAIN "welcome to" (e.g. "Welcome to Chiming") must
# never be rejected on that basis; only an exact match against a known
# server-default/parking-page name is high-confidence enough to reject
# outright. Ambiguous cases are caught by _PARKING_PAGE_TEXT_SIGNATURES
# below instead of by growing this list.
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

# Checked as a substring against the fetched PAGE TEXT, not the
# extracted name -- these are real server-default/parking-page
# boilerplate phrases, so substring matching here doesn't carry the
# same false-positive risk a name blocklist substring match would.
_PARKING_PAGE_TEXT_SIGNATURES: tuple = (
    "the nginx web server is successfully installed",
    "apache2 ubuntu default page",
    "apache2 debian default page",
    "this is the default welcome page",
    "the web server software is running but no content has been added",
    "this domain is parked",
    "domain is parked",
    "buy this domain",
    "this domain is for sale",
    "godaddy.com",
    "namecheap parking",
    "further configuration is required",
)

# A page with less real content than this (after stripping whitespace)
# isn't a real company page regardless of what name was extracted from it.
_MIN_MEANINGFUL_PAGE_TEXT_LENGTH = 60


def _parking_page_reason(page_text: str) -> Optional[str]:
    """None if `page_text` looks like a real page; otherwise a
    human-readable reason it looks like a server-default/parking/
    holding page. Name-agnostic -- reused by both name extraction
    (_reject_reason_for_extracted_name) and address extraction
    (_attempt_address_extraction) as a pre-filter, since a junk page
    can produce a junk answer for either kind of extraction."""
    haystack = (page_text or "").lower()
    for signature in _PARKING_PAGE_TEXT_SIGNATURES:
        if signature in haystack:
            return f"page text matches a known server-default/parking-page signature ('{signature}')"

    if len(re.sub(r"\s+", "", page_text or "")) < _MIN_MEANINGFUL_PAGE_TEXT_LENGTH:
        return "page text is too short to be a real company page"

    return None


def _reject_reason_for_extracted_name(name: str, page_text: str) -> Optional[str]:
    """None if `name` passes the floor test; otherwise a human-readable
    rejection reason. See the constants above for what's checked and
    why the name check is whole-string, not substring."""
    normalised_name = " ".join(name.strip().lower().rstrip("!.").split())
    if normalised_name in _JUNK_NAME_EXACT_BLOCKLIST:
        return f"extracted name '{name}' matches a known server-default/placeholder page name"

    return _parking_page_reason(page_text)


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


def _address_candidate_sources(pages: List[Any]) -> List[tuple]:
    """Ordered (tier_label, url, text) candidates for address
    extraction -- contact page, footer text, impressum page, per the
    required preference order. Only the first page found in each tier
    is used (at most one candidate per tier, so at most 3 LLM calls
    total per row -- see _attempt_address_extraction, which stops at
    the first tier that actually yields an address)."""
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
    ):
        self.repo = repo or SupplierRepository()
        self.matcher = matcher or SupplierMatcher(self.repo)
        self.collection_service = collection_service or CollectionService(repo=self.repo)
        self.llm_client = llm_client or LLMClient()

    def run_batch(
        self, rows: List[ParsedRow], batch_job_id: str,
        progress_callback: Optional[Callable[[BatchOutcome], None]] = None,
    ) -> BatchOutcome:
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
            if _parking_page_reason(text):
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
