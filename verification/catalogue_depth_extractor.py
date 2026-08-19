"""
verification/catalogue_depth_extractor.py

Reads a supplier's own website pages (already fetched -- see
verification/catalogue_depth_service.py for where the text comes from)
and extracts catalogue-depth signals: whether the site shows real
customer/client evidence rather than generic marketing copy alone.
Three signal types, each requiring a real quoted/described detail from
the page, never inferred:

  - customer_logos_section: a section of the page displaying named
    customer/client/partner logos or names (e.g. "Trusted by: Acme
    Corp, Globex, Initech" or an explicit "Our Clients" logo strip).
  - named_case_studies: a specific, named project or customer case
    study -- not "we've helped many clients," but an actual named
    company, product, or project described in some detail.
  - specific_process_detail: concrete, specific manufacturing detail --
    named machine models, tonnage, cycle times, a named certification
    process, specific tooling -- as opposed to generic marketing
    language ("state-of-the-art facility", "cutting-edge technology")
    that names nothing. This is the one place "vs. generic marketing
    language" from the original ask isn't a separate judgment call:
    generic copy simply has nothing concrete to quote, so it produces
    no finding, same as every other "omit rather than guess" gate in
    this codebase.

Evidence only, for a human to weigh manually when answering the buyer
tracker's own criterion A (Website Deep-Dive) -- this module has no
opinion on whether a supplier "passes" A, and nothing downstream of it
writes anything into that column. Same discipline as
capability_extractor.py, whose class shape and grounded-quote-required
prompt style this mirrors closely (same never-raises contract, same
"empty array is a valid, correct answer" rule).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"  # NOT gpt-4o-mini, unlike capability_extractor.py -- confirmed live: mini returned zero
# findings on a real page whose homepage literally states "26 INJECTION MOLDING MACHINES (80T TO 850T)"
# and "68 CNC MACHINES", both textbook specific_process_detail evidence. Same prompt, same page, gpt-4o
# correctly found both plus two more real signals. Not a prompt-wording issue -- a genuine capability
# gap on this specific judgment call (distinguishing real specifics from marketing copy in messy scraped
# stat-card text). Costs more per supplier than mini; worth it here since mini's output would otherwise
# be systematically wrong (silently near-zero), not just noisier.

VALID_SIGNAL_TYPES = ("customer_logos_section", "named_case_studies", "specific_process_detail")

SYSTEM_PROMPT = """List everything on this page that is one of these three things:

1. customer_logos_section -- named customers, clients, or partners (a logo strip, "Trusted by X, Y, Z", a client list naming real companies).
2. named_case_studies -- a specific, named project, customer, or case study described in some detail.
3. specific_process_detail -- a concrete, specific manufacturing fact: a machine model/brand, a machine count, tonnage/capacity, a certification actually held (e.g. "ISO 9001:2015"), a named process (e.g. "EDM machining", "laser cutting"), employee count, factory size, export volume, years in business, or any other specific named/numeric detail about the company's operations. Include stat-card numbers (e.g. "80 employees", "6,000 sqm facility"), not just prose.

Skip vague marketing language with no specific name or number ("state-of-the-art", "trusted by leading brands", "cutting-edge technology").

Return a JSON array. Each item: {"signal_type": one of the three types above, "evidence": a short quote or close paraphrase from the page, under 30 words}. Return [] if the page has none of these. A page commonly has several -- list all of them, not just one.
"""


@dataclass(frozen=True)
class CatalogueSignalFinding:
    signal_type: str
    evidence: str
    source_url: str


def _parse_model_json(text: str) -> List[dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError(f"expected a JSON array, got {type(parsed).__name__}")
    return [item for item in parsed if isinstance(item, dict)]


def _finding_from_assertion(assertion: dict, *, source_url: str) -> Optional[CatalogueSignalFinding]:
    signal_type = assertion.get("signal_type")
    evidence = assertion.get("evidence")

    if signal_type not in VALID_SIGNAL_TYPES:
        return None
    if not isinstance(evidence, str) or not evidence.strip():
        return None

    return CatalogueSignalFinding(signal_type=signal_type, evidence=evidence.strip(), source_url=source_url)


class CatalogueDepthExtractor:
    """Same injectable-client, never-raises design as CapabilityExtractor
    (its direct template): construction never requires an API key, only
    calling `extract` does, and any failure (network, parsing) returns
    an empty list with a logged error rather than propagating -- one
    supplier's bad page must never abort a batch run."""

    def __init__(self, client: Optional[Any] = None, model: str = DEFAULT_MODEL):
        self.model = model
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # imported lazily: optional dep for tests

            self._client = OpenAI()  # reads OPENAI_API_KEY from the environment
        return self._client

    def extract(self, page_text: str, *, source_url: str = "") -> List[CatalogueSignalFinding]:
        """Extract catalogue-depth signal findings from one page's
        text. Returns `[]` on a genuinely empty/generic page (a valid
        outcome) and also on any failure to reach or parse the model's
        response -- check the logs if you need to tell them apart in
        bulk."""
        if not page_text or not page_text.strip():
            return []

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1000,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Website page content:\n\n{page_text[:20_000]}"},
                ],
            )
            text = response.choices[0].message.content or ""
            assertions = _parse_model_json(text)
        except Exception as e:
            logger.error("Catalogue-depth extraction failed for %s: %s", source_url or "<no url>", e)
            return []

        findings = []
        for assertion in assertions:
            finding = _finding_from_assertion(assertion, source_url=source_url)
            if finding is not None:
                findings.append(finding)
        return findings

    def extract_from_pages(self, pages: List[Any]) -> List[CatalogueSignalFinding]:
        """Convenience wrapper over multiple `.url`/`.text`-shaped page
        objects -- anything from `scrapers.own_website_scraper.OwnWebsitePage`
        to the lightweight namedtuple-ish pages
        `verification.catalogue_depth_service` builds from on-disk
        collection HTML. Findings for the identical (signal_type,
        evidence) pair across two pages are not deduplicated here --
        `storage.repository.add_catalogue_signal_finding`'s own insert
        is the deduplication point, so this stays a plain, stateless
        per-page map."""
        all_findings: List[CatalogueSignalFinding] = []
        for page in pages:
            all_findings.extend(self.extract(page.text, source_url=page.url))
        return all_findings
