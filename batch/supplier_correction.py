"""
batch/supplier_correction.py

Reusable fix for a bad supplier record (e.g. a false-match domain a
validation gap let through -- see scrapers/company_website_finder.py's
own corroboration guards), built to correct the Ashpock -> shpock.com
and IK Eng Ltd -> easydigitalfiling.com false matches -- so the next
bad record gets the same audited correction path instead of another
hand-applied patch. Every correction goes through the same real
pipeline every other write in this codebase does (CompanyWebsiteFinder
+ CollectionService), with a supplier_change_log entry, never a raw
SQL patch.

Extracted out of main.py's `correct-supplier` CLI command so both that
command AND POST /suppliers/{id}/correct-domain (api/app.py) share one
tested implementation, matching this codebase's "thin wrapper, real
logic lives in a service module" discipline. The API endpoint exists
specifically because this codebase's production database is a SQLite
file on a Railway volume, not network-reachable -- the CLI command can
only ever run against a database on the SAME filesystem it's invoked
from, so an already-deployed bad record can only be corrected over
HTTP, through the running service itself.

Two correction modes: `correct_domain` clears a wrong value and
re-resolves it via search (CompanyWebsiteFinder) -- for when the
domain alone was wrong but the search term (company name) is right.
`set_confirmed_domain` writes an already-human-verified domain/name
directly, no search -- for when the ORIGINAL search term was itself
wrong (a fresh search under the same wrong name just re-surfaces the
same wrong candidate). Add another `correct_<field>`/`set_<field>`
method here for a future bad-record class, following the same shape,
rather than reaching for raw SQL.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from storage.repository import SupplierRepository


class SupplierCorrectionService:

    def __init__(
        self,
        repo: Optional[SupplierRepository] = None,
        website_finder: Optional[Any] = None,
        collection_service: Optional[Any] = None,
    ):
        self.repo = repo or SupplierRepository()
        if website_finder is not None:
            self.website_finder = website_finder
        else:
            from scrapers.company_website_finder import CompanyWebsiteFinder
            from scrapers.google_search_scraper import GoogleSearchScraper
            from scrapers.own_website_scraper import OwnWebsiteScraper

            self.website_finder = CompanyWebsiteFinder(GoogleSearchScraper(), OwnWebsiteScraper())
        if collection_service is not None:
            self.collection_service = collection_service
        else:
            from collection.collection_service import CollectionService

            self.collection_service = CollectionService(repo=self.repo)

    def correct_domain(self, supplier_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
        """Clears `domain` (logged via SupplierRepository.clear_supplier_field,
        changed_by="manual"), re-resolves it through CompanyWebsiteFinder
        (the exact same real search+validation path find-websites/POST
        /companies/enrich already use), and re-collects from the
        corrected site if one validates. Costs one paid SerpAPI call,
        plus a small OpenAI call if a candidate site is found and needs
        the grounded-name corroboration check.

        Returns a plain dict describing what happened -- never raises
        for an ordinary "no replacement found" outcome (status
        "needs_url"), only for a genuinely missing supplier_id."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        old_domain = supplier.get("domain")
        canonical_name = supplier.get("canonical_name")

        cleared = False
        if old_domain:
            clear_reason = reason or f"manual correction: {old_domain!r} confirmed to be a wrong/unrelated site"
            self.repo.clear_supplier_field(supplier_id, "domain", changed_by="manual", change_reason=clear_reason)
            cleared = True

        finding = self.website_finder.find_website(canonical_name, country=supplier.get("country"))

        if not finding.validated:
            return {
                "supplier_id": supplier_id, "canonical_name": canonical_name,
                "status": "needs_url", "old_domain": old_domain, "new_domain": None,
                "cleared": cleared, "reason": finding.reason,
            }

        resolve_reason = f"corrected from wrong domain {old_domain!r}: {finding.reason}"
        self.repo.update_supplier_fields_with_history(
            supplier_id, {"domain": finding.domain}, changed_by="manual", change_reason=resolve_reason,
        )
        collect_outcome = self.collection_service.collect(supplier_id, source_url=finding.domain)

        return {
            "supplier_id": supplier_id, "canonical_name": canonical_name,
            "status": "resolved", "old_domain": old_domain, "new_domain": finding.domain,
            "cleared": cleared, "reason": finding.reason,
            "collection_status": collect_outcome.get("status"),
            "pages_visited": collect_outcome.get("pages_visited", 0),
        }

    def set_confirmed_domain(
        self, supplier_id: int, domain: str, canonical_name: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """For when a human has ALREADY confirmed the correct domain
        (and optionally the correct name) directly -- correct_domain's
        own search-based re-resolution is the wrong tool here: the
        ORIGINAL search term can itself be wrong (real case: "Ashpock"
        was a misspelling of "Aspock"/"Aspoeck" -- re-searching the
        same wrong name just re-surfaces the same wrong candidate, or
        nothing at all), so a fresh search adds cost and risk for no
        benefit once a human has already done the real verification.
        Writes both fields directly via
        SupplierRepository.update_supplier_fields_with_history
        (changed_by="manual", auditable exactly like every other write
        here), then re-collects from the confirmed domain."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        old_domain = supplier.get("domain")
        old_name = supplier.get("canonical_name")

        fields: Dict[str, Any] = {"domain": domain}
        if canonical_name and canonical_name != old_name:
            fields["canonical_name"] = canonical_name

        set_reason = reason or f"manual correction: confirmed correct domain set directly (was {old_domain!r})"
        self.repo.update_supplier_fields_with_history(
            supplier_id, fields, changed_by="manual", change_reason=set_reason,
        )
        collect_outcome = self.collection_service.collect(supplier_id, source_url=domain)

        return {
            "supplier_id": supplier_id,
            "canonical_name": canonical_name or old_name, "old_canonical_name": old_name,
            "status": "set", "old_domain": old_domain, "new_domain": domain,
            "collection_status": collect_outcome.get("status"),
            "pages_visited": collect_outcome.get("pages_visited", 0),
        }
