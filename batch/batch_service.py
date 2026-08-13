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
  -- attempts to replace the placeholder with a real one; if found, the
  value AND its provenance (source_url, raw_snippet, extraction_method,
  source_tier='own_domain', claim_type='verifiable_fact') are recorded
  via field_provenance, since it's an extracted value like any other
  and belongs in the calibration loop the same way.
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
"""

from __future__ import annotations

import logging
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


@dataclass
class BatchOutcome:
    total_rows: int = 0
    needs_url: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    placeholder_names_used: int = 0
    placeholder_names_replaced: int = 0


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
                collect_result = self.collection_service.collect(
                    supplier_id, return_pages=(name_source == "inferred_from_domain"),
                    source_url=row.website,
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
                if self._attempt_name_extraction(supplier_id, collect_result, batch_row_id):
                    outcome.placeholder_names_replaced += 1

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

    def _attempt_name_extraction(self, supplier_id: int, collect_result: Dict[str, Any], batch_row_id: int) -> bool:
        """Reuses discovery.candidate_validator.SYSTEM_PROMPT verbatim --
        same grounded-only extraction discipline, applied here to a
        page already fetched for collection rather than a fresh one.
        Returns True only if a real name was found and stored.

        Updates BOTH the supplier record (canonical_name, the
        authoritative value) AND this batch row's own company_name --
        the row-level copy exists so a row that's later re-queried in
        isolation (e.g. by the export or a review view) doesn't have to
        join back to suppliers just to see what was actually found;
        csv_exporter.py still prefers the live supplier value as the
        ultimate source of truth, this is a consistency nicety, not the
        only place the real value lives."""
        pages = collect_result.get("pages") or []
        if not pages:
            return False
        page = pages[0]
        page_text = getattr(page, "text", "") or ""
        if not page_text.strip():
            return False

        try:
            extracted = self.llm_client.complete_json(
                NAME_EXTRACTION_SYSTEM_PROMPT, f"Website page content:\n\n{page_text[:20_000]}",
            )
        except Exception as e:  # noqa: BLE001 -- an extraction failure must never fail an otherwise-successful collection
            logger.warning("batch: name extraction failed for supplier #%s: %s", supplier_id, e)
            return False
        if not isinstance(extracted, dict):
            return False

        name = extracted.get("company_name")
        if not isinstance(name, str) or not name.strip():
            return False
        name = name.strip()

        self.repo.update_supplier_fields_with_history(
            supplier_id, {"canonical_name": name},
            changed_by="batch_service",
            change_reason="replaced domain-derived placeholder with a name explicitly found on the supplier's own site",
        )
        self.repo.save_field_provenance(
            supplier_id=supplier_id, field_name="canonical_name", value=name,
            source_url=getattr(page, "url", None), raw_snippet=page_text[:500],
            extraction_method="llm_grounded_extraction",
            source_tier="own_domain", claim_type="verifiable_fact",
        )
        self.repo.update_batch_upload_row(batch_row_id, {"company_name": name})
        return True
