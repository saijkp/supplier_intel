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

Correction modes: `correct_domain` clears a wrong value and
re-resolves it via search (CompanyWebsiteFinder) -- for when the
domain alone was wrong but the search term (company name) is right.
`set_confirmed_domain` writes an already-human-verified domain/name
directly, no search -- for when the ORIGINAL search term was itself
wrong (a fresh search under the same wrong name just re-surfaces the
same wrong candidate). `set_product_keywords` backfills a supplier's
missing category tag directly, guarded so it only ever fills an empty
value, never overwrites one. `set_canonical_name` corrects an
already-populated name directly, no search, no re-collection --
deliberately unguarded (a human-directed overwrite of a value already
known to be wrong, not a backfill). Add another `correct_<field>`/
`set_<field>` method here for a future bad-record class, following the
same shape, rather than reaching for raw SQL.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

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
        here), then re-collects from the confirmed domain.

        `domain` is UNIQUE on `suppliers` -- real case, found live:
        setting "Ashpock" (a bad duplicate record) to aspoeck.com
        failed outright, because the REAL Aspoeck Systems already has
        its own supplier row under that exact domain (unsurprising --
        trailer lighting is a real product category this platform
        already scrapes). That's not an error to retry differently,
        it's proof the bad record IS a duplicate of an
        already-existing real one -- returned as status
        "domain_conflict" (naming the existing row, via
        SupplierRepository.find_by_domain) instead of letting a raw
        sqlite3.IntegrityError surface as an opaque job failure. See
        flag_duplicate for the correct next step once that's
        confirmed."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        old_domain = supplier.get("domain")
        old_name = supplier.get("canonical_name")

        fields: Dict[str, Any] = {"domain": domain}
        if canonical_name and canonical_name != old_name:
            fields["canonical_name"] = canonical_name

        set_reason = reason or f"manual correction: confirmed correct domain set directly (was {old_domain!r})"
        try:
            self.repo.update_supplier_fields_with_history(
                supplier_id, fields, changed_by="manual", change_reason=set_reason,
            )
        except sqlite3.IntegrityError:
            existing = self.repo.find_by_domain(domain)
            existing_desc = (
                f"#{existing['id']} ({existing.get('canonical_name')!r})" if existing else "another supplier"
            )
            return {
                "supplier_id": supplier_id, "canonical_name": old_name,
                "status": "domain_conflict", "old_domain": old_domain, "new_domain": None,
                "reason": f"{domain!r} already belongs to {existing_desc} -- this record is likely "
                          f"a duplicate; see flag_duplicate rather than forcing the domain",
                "conflicting_supplier_id": existing["id"] if existing else None,
            }
        collect_outcome = self.collection_service.collect(supplier_id, source_url=domain)

        return {
            "supplier_id": supplier_id,
            "canonical_name": canonical_name or old_name, "old_canonical_name": old_name,
            "status": "set", "old_domain": old_domain, "new_domain": domain,
            "collection_status": collect_outcome.get("status"),
            "pages_visited": collect_outcome.get("pages_visited", 0),
        }

    def flag_duplicate(self, supplier_id: int, flag_reason: str) -> Dict[str, Any]:
        """Marks a bad record excluded (CLAUDE.md standing rule 8:
        excluded suppliers are flagged, never deleted, so they can
        never silently resurface in a future search/export) -- the
        correct resolution for a false-match record that turns out to
        be a duplicate of an already-existing real supplier (see
        set_confirmed_domain's own "domain_conflict" status: the
        UNIQUE constraint on suppliers.domain is what surfaces this,
        found live setting the "Ashpock" record to aspoeck.com when
        the real Aspoeck Systems already had its own row under that
        domain)."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        self.repo.update_supplier_fields_with_history(
            supplier_id, {"flagged": True, "flag_reason": flag_reason},
            changed_by="manual", change_reason=flag_reason,
        )
        return {
            "supplier_id": supplier_id, "canonical_name": supplier.get("canonical_name"),
            "status": "flagged", "flag_reason": flag_reason,
        }

    def set_product_keywords(
        self, supplier_id: int, product_keywords: List[str], reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Backfills product_keywords on a supplier whose category
        membership is already a trusted fact (e.g. a checked-in,
        audited category roster -- see batch/category_roster.py) but
        which was never populated with a matching search term, making
        it invisible to storage.repository.search_suppliers_full's
        product-term LIKE match and therefore to
        discovery.discovery_service.discover_to_target's Phase 0
        database-first check. No search, no re-collection needed --
        unlike domain, product_keywords is a pure category tag, never
        something a fetch populates.

        Trusted-value guard is enforced HERE, not left to the caller to
        remember (closing a real gap in how this was first done: an
        earlier one-off backfill relied on the caller pre-filtering for
        `product_keywords IS NULL` before calling) -- re-fetches the
        supplier fresh and checks at write time, so a value populated
        by unrelated real activity between the caller deciding to call
        this and the call actually landing is never silently
        clobbered. Returns status="skipped_not_empty" (existing value
        preserved, nothing written) rather than overwriting."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        existing = supplier.get("product_keywords")
        if existing:
            return {
                "supplier_id": supplier_id, "canonical_name": supplier.get("canonical_name"),
                "status": "skipped_not_empty", "existing_product_keywords": existing,
            }

        set_reason = reason or "backfill: confirmed category-roster member with empty product_keywords"
        self.repo.update_supplier_fields_with_history(
            supplier_id, {"product_keywords": product_keywords},
            changed_by="manual", change_reason=set_reason,
        )
        return {
            "supplier_id": supplier_id, "canonical_name": supplier.get("canonical_name"),
            "status": "set", "product_keywords": product_keywords,
        }

    def set_canonical_name(
        self, supplier_id: int, canonical_name: str, reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Corrects a supplier's canonical_name directly -- for when a
        human has already confirmed the real name (e.g. against the
        original source listing, or a Companies House match whose
        free-text search only succeeded once a hybrid/invented name was
        replaced with the real one -- see the Lakeland Tankers fix this
        session) and the fix is purely a naming correction, not a
        domain problem. Deliberately a direct, unguarded overwrite --
        unlike set_product_keywords (which only ever fills an empty
        value), a name correction is explicitly replacing an already-
        populated field a human has determined to be wrong, the same
        "human already verified this, just write it" semantics
        set_confirmed_domain uses for domain. No search, no
        re-collection -- reusing set_confirmed_domain here (passing the
        unchanged domain just to smuggle in a name fix) would trigger a
        real, unnecessary re-collection for a fact that doesn't need
        one, the same reasoning set_product_keywords's own docstring
        gives for not reusing correct_domain."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id={supplier_id}")

        old_name = supplier.get("canonical_name")
        set_reason = reason or f"manual correction: canonical_name corrected (was {old_name!r})"
        self.repo.update_supplier_fields_with_history(
            supplier_id, {"canonical_name": canonical_name},
            changed_by="manual", change_reason=set_reason,
        )
        return {
            "supplier_id": supplier_id, "canonical_name": canonical_name,
            "old_canonical_name": old_name, "status": "set",
        }
