"""
verification/address_extractor.py

Grounded, tiered-candidate-source address extraction from a supplier's
own website pages -- extracted out of batch/batch_service.py (which
still uses it, unchanged in behavior) so
sourcing/sourcing_agent.py can reuse the exact same logic instead of
duplicating it. Found live while investigating the Source tab's
misleading ai_confidence_score/procurement_recommendation display:
sourcing_agent.py's own candidate path never populated
suppliers.address at all, which meant
verification_ai/cross_checker.py's facility_address sub-check (25 of
confidence_scorer.py's ~100 weighted points) could never fire for a
Source-tab candidate -- not a scoring bug, an upstream missing input.

Same trusted-value-guard discipline as every other extraction in this
codebase (CLAUDE.md standing rule 4): only ever fills suppliers.address
when it's currently empty; a disagreement with an already-trusted
value is recorded via field_provenance (field_name="address_candidate"),
never applied.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from verification.website_contact_extractor import parking_page_reason

logger = logging.getLogger(__name__)

# A page with less real content than this (after stripping whitespace)
# isn't trustworthy enough for a grounded LLM claim (a name or an
# address) regardless of what was extracted from it. NOT part of
# parking_page_reason itself -- that's shared with contact extraction,
# which has no equivalent length floor (a contact page can legitimately
# be short: just an address/phone block), unlike a page an LLM is being
# asked to assert a company name or address from.
_MIN_MEANINGFUL_PAGE_TEXT_LENGTH = 60


def reject_reason_for_llm_extraction(page_text: str) -> Optional[str]:
    """None if `page_text` is trustworthy enough to run a grounded LLM
    extraction (name or address) against; otherwise a reason. Layers
    this module's own length floor on top of the shared, signature-only
    parking_page_reason."""
    reason = parking_page_reason(page_text)
    if reason:
        return reason
    if len(re.sub(r"\s+", "", page_text or "")) < _MIN_MEANINGFUL_PAGE_TEXT_LENGTH:
        return "page text is too short to be a real company page"
    return None


# Grounded-only, same discipline as discovery/candidate_validator.py's
# SYSTEM_PROMPT -- critically, rule 2 is what makes "store the city,
# leave the rest empty" the default behaviour rather than something
# callers have to special-case: the model is told to return exactly
# the substring found, never to complete a partial address.
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


def address_candidate_sources(pages: List[Any]) -> List[tuple]:
    """Ordered (tier_label, url, text) candidates for address
    extraction -- contact page, footer text, impressum page, about
    page, per the required preference order. Only the first page found
    in each tier is used (at most one candidate per tier, so at most 4
    LLM calls total per call -- see attempt_address_extraction, which
    stops at the first tier that actually yields an address).

    "about page": a general company-info page is the LEAST authoritative
    of the four tiers (a dedicated contact/impressum page states an
    address on purpose; an about page mentions one incidentally, if at
    all) -- deliberately tried last, only once contact/footer/impressum
    have all come up empty. Matches "about" OR "company" in the URL --
    real sites use both conventions for the same page (e.g.
    plasticmold.net/company/, hordrt.com/about-us-3/)."""
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


def attempt_address_extraction(
    repo: Any, llm_client: Any, supplier_id: int, pages: List[Any], *, changed_by: str,
) -> str:
    """Runs against whatever `pages` the caller already fetched (no new
    fetch here) -- address isn't tied to whether the caller has a
    company name. Tries candidate sources in the order specified,
    stopping at the first tier that actually yields an address (never
    blending across tiers, never making more than one LLM call per
    tier).

    Returns "applied" (address was empty, now written to
    suppliers.address + field_provenance), "conflicting" (supplier
    already had a non-empty address from elsewhere -- never
    overwritten, but the extracted value is still recorded via
    field_provenance under field_name="address_candidate" so a
    disagreement between the trusted address and the site's own
    content is visible), or "skipped" (no pages, every tier
    empty/parking-page-shaped, or no address found anywhere).

    `changed_by` is threaded into the supplier_change_log entry so the
    audit trail shows which caller actually wrote it (e.g.
    "batch_service" vs "sourcing_agent")."""
    if not pages:
        return "skipped"

    for tier_label, url, text in address_candidate_sources(pages):
        if reject_reason_for_llm_extraction(text):
            continue
        try:
            extracted = llm_client.complete_json(
                ADDRESS_EXTRACTION_SYSTEM_PROMPT,
                f"Website page content ({tier_label}):\n\n{text[:20_000]}",
            )
        except Exception as e:  # noqa: BLE001 -- an extraction failure must never fail an otherwise-successful caller
            logger.warning("address_extractor: extraction failed for supplier #%s (%s): %s", supplier_id, tier_label, e)
            continue
        if not isinstance(extracted, dict):
            continue

        address = extracted.get("address")
        if not isinstance(address, str) or not address.strip():
            continue
        address = address.strip()

        supplier = repo.get_supplier(supplier_id)
        current_address = (supplier or {}).get("address")

        if current_address:
            logger.info(
                "address_extractor: extracted address for supplier #%s (%s) conflicts with existing "
                "address -- not applied", supplier_id, tier_label,
            )
            repo.save_field_provenance(
                supplier_id=supplier_id, field_name="address_candidate", value=address,
                source_url=url, raw_snippet=text[:500],
                extraction_method="llm_grounded_extraction",
                source_tier="own_domain", claim_type="verifiable_fact",
            )
            return "conflicting"

        repo.update_supplier_fields_with_history(
            supplier_id, {"address": address},
            changed_by=changed_by,
            change_reason=f"address found on the supplier's own site ({tier_label})",
        )
        repo.save_field_provenance(
            supplier_id=supplier_id, field_name="address", value=address,
            source_url=url, raw_snippet=text[:500],
            extraction_method="llm_grounded_extraction",
            source_tier="own_domain", claim_type="verifiable_fact",
        )
        return "applied"

    return "skipped"
