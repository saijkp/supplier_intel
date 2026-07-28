"""
verification/capability_extractor.py

Reads a supplier's own website text and extracts which manufacturing
processes/capabilities/standards they genuinely operate in-house — as
distinct from what they merely sell.

Why this is a fifth signal, not a fix to an existing one
------------------------------------------------------------
`ManufacturerVerifier` already distinguishes manufacturer from trader
using government registry data (business scope, registered capital)
and platform metadata (tenure, certifications) — genuinely strong
signals, but all of them answer "is this company *a* manufacturer,"
never "does this company make *this specific process*." A supplier
correctly assessed as a real manufacturer might still not be the
rotomoulder your enquiry needs — they might manufacture stamped metal
brackets, not plastic parts. This module answers the specific-process
question, reading the one source that actually states it in the
company's own words: their own website.

The trading-company trap, and why exact wording matters
------------------------------------------------------------
A directory listing and a manufacturer's own site can describe the
exact same rotomoulded toolbox in identical words — the words don't
distinguish "we make this" from "we sell this." The prompt below is
built specifically to make that distinction and never conflate them:
sold-only observations produce no finding at all. Confusing the two is
the single failure that would make this module actively harmful rather
than merely imprecise — it is worth reading the prompt closely before
changing it.

Model choice
--------------
Every other AI-assisted module in this codebase (`product_matcher`,
`factory_photo_verifier`) defaults to `gpt-4o` because they need
vision. This is a pure text-classification task run once per supplier
across a catalogue that's meant to reach 10,000 — `gpt-4o-mini` is the
deliberately cheaper default here for that reason. Confirm it's still
current and adequate for this task before relying on it at scale; this
default has not been validated against a live API call in this
environment (same disclosed limitation as `factory_photo_verifier`).

NOT YET WIRED INTO ManufacturerVerifier.assess()
----------------------------------------------------
Matching this codebase's own established practice for
`factory_photo_verifier` (see that module's docstring): a new signal
ships standalone first, with its own assessor function
(`assess_own_website_capability`, below) in the same
`(verdict, explanation)` shape every existing assessor in
`manufacturer_verifier.py` uses — so wiring it into `assess()`'s
weighted aggregation later is a one-line addition to that module's
`checks` dict and `ASSESSOR_WEIGHTS`, not a redesign. Not done in this
change because rebalancing the four existing weights to make room for
a fifth would change `ManufacturerVerifier.assess()`'s output for
every supplier already scored under the current weights, and that's a
decision worth making deliberately, not a side effect of adding this
module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from verification.capability_vocabulary import CATEGORY_STANDARD, CapabilityTerm, map_to_canonical

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"  # CONFIRM this is still current before relying on it — see module docstring

RELATIONSHIP_IN_HOUSE = "in_house"
RELATIONSHIP_SUBCONTRACTED = "subcontracted"
RELATIONSHIP_SOLD_ONLY = "sold_only"
RELATIONSHIP_ASSERTED = "asserted"

# Deterministic check, keyed by canonical term: the evidence text must
# actually contain one of that term's own markers, not just adjacent
# marketing language. Added after a real calibration finding that
# proved the prompt instruction alone insufficient -- telling the
# model "don't infer ISO 9001 from vague quality language" as one rule
# among several did not reliably stop it from doing exactly that
# (found via a real supplier's site: "known as a quality leader" kept
# getting cited as evidence for ISO 9001 even after that prompt rule
# was added). A model instruction is a request it can ignore; this
# check cannot be argued around by phrasing.
#
# A second Automechanika calibration pass found the identical failure
# mode outside standards too: generic export/relationship marketing
# copy ("preferred supplier for international distributors", "building
# trusting client relationships") was being cited as evidence for a
# specific logistics or market-presence claim it never actually makes.
# One page's vague "distributed to dozens of countries worldwide" was
# even reused as "evidence" for three different specific regional
# claims (UK, North America, Australia) at once — a sentence naming no
# region cannot be genuine evidence for any of them. Extended here,
# same mechanism, same reasoning.
#
# "serves europe market" deliberately has no entry (yet) -- unlike the
# other market-presence terms, its own real-world evidence tends to be
# a list of specific countries (see capability_vocabulary.py's note on
# that term), and a robust marker set for that shape needs more
# calibration data than one pass has produced so far.
_EVIDENCE_MUST_CONTAIN = {
    # --- Standards ---
    "iso 9001": ("9001",),
    "iatf 16949": ("16949",),
    "iso 14001": ("14001",),
    "ce marking": ("ce mark", "ce certif", "ce complian", "ce"),

    # --- Logistics ---
    "ddp shipping": ("ddp", "delivered duty paid"),
    "dap shipping": ("dap", "delivered at place"),
    "fob shipping": ("fob", "free on board"),
    "cif shipping": ("cif", "cost insurance and freight"),
    "door to door shipping": ("door to door",),
    "uk warehouse": (
        "uk warehouse", "warehouse in the uk", "uk stock",
        "uk distribution centre", "uk distribution center", "uk based warehouse",
    ),
    "eu warehouse": (
        "eu warehouse", "warehouse in europe", "european warehouse", "eu stock",
        "eu distribution centre", "eu distribution center",
    ),
    "customs expertise": ("customs",),

    # --- Market presence (region terms only — "oem supplier" is a
    # status claim, not a "names a specific place" claim, and every
    # OEM-supplier finding in the calibration pass was well supported,
    # so it isn't included here) ---
    "serves uk market": ("uk", "united kingdom"),
    "serves north america market": ("north america", "usa", "canada", "canadian"),
    "serves australia market": ("australia", "australian"),
}

_EVIDENCE_PUNCTUATION_CHARS = ("-", ".", ",", "/", "(", ")", ";", ":", "!", "?", '"', "'")

# These three markers are deliberately matched as a *prefix*, not a
# whole word -- "ce certif" is meant to match "certified"/"certificate"/
# "certification" alike, so it can't also require a boundary right
# after "certif". Every other marker (including the bare "ce" below,
# which specifically must NOT match inside "certificate") gets a
# boundary on both sides.
_PREFIX_ONLY_MARKERS = {"ce mark", "ce certif", "ce complian"}


def _evidence_has_required_marker(evidence: str, required_markers: Tuple[str, ...]) -> bool:
    """Word/phrase match against normalised evidence text — deliberately
    not a naive substring check: "cif" as a bare substring matches
    inside "specific", "usa" matches inside "usage". Punctuation is
    folded to spaces first so a marker at the end of a sentence
    ("...DDP.") or wrapped in parentheses ("(FOB)") still gets a clean
    boundary rather than being missed.
    """
    text = evidence.strip().lower()
    for ch in _EVIDENCE_PUNCTUATION_CHARS:
        text = text.replace(ch, " ")
    text = f" {' '.join(text.split())} "
    for marker in required_markers:
        trailing_boundary = r"" if marker in _PREFIX_ONLY_MARKERS else r"\b"
        if re.search(r"\b" + re.escape(marker) + trailing_boundary, text):
            return True
    return False


# 'asserted' (v9, commercial-intelligence extension): for claims with
# no in-house-vs-subcontracted distinction at all -- "we serve the UK
# market" isn't something a company does in-house or subcontracts,
# it's just true or not. See capability_vocabulary's
# CATEGORY_MARKET_PRESENCE terms, which always use this relationship.
_VALID_RELATIONSHIPS = (
    RELATIONSHIP_IN_HOUSE, RELATIONSHIP_SUBCONTRACTED,
    RELATIONSHIP_SOLD_ONLY, RELATIONSHIP_ASSERTED,
)

SYSTEM_PROMPT_TEMPLATE = """You are a procurement analyst assessing what a company genuinely does, \
reading one page of that company's own website — both manufacturing processes and commercial or \
logistics facts (shipping terms, markets served, OEM readiness).

Return ONLY a JSON array. No preamble, no markdown fences, no commentary. Each element must be an \
object with exactly these keys:

  "term":         string. The capability or fact, preferably one of the known terms listed below.
  "relationship": one of "in_house", "subcontracted", "sold_only", "asserted".
  "evidence":     string. A short direct quotation from the page, under 25 words, supporting this.
  "confidence":   number between 0 and 1. Your genuine certainty. Do not default or inflate it.

Rules, in priority order:

1. DISTINGUISH MAKING FROM SELLING, for manufacturing processes and capabilities. A page listing \
products for sale proves nothing about how they are produced. Only use "in_house" when the page \
asserts the company itself operates the process — describing its own machines, workshop, \
production lines, or staff. If the page only shows the company supplies or offers such products, \
use "sold_only".
2. Use "subcontracted" when the page states the work is done by partners or a supplier network — \
this applies to logistics and OEM-readiness facts too (e.g. shipping handled by a named freight \
partner, not the company itself).
3. Use "asserted" for facts with no in-house-vs-subcontracted distinction at all — which markets \
the company serves, whether they describe themselves as an OEM/Tier-1 supplier, and similar plain \
factual claims. Never use "sold_only" for these; that value is only for manufacturing capabilities. \
Certifications and standards (ISO 9001, IATF 16949, CE marking, ...) are also always "asserted" — a \
company either holds a certification or it doesn't, there is no in-house-vs-subcontracted version of \
holding one.
4. FOR CERTIFICATIONS AND STANDARDS SPECIFICALLY, the evidence must name the actual standard — "ISO \
9001", "IATF 16949", "CE marking", or an equally specific reference. General quality language ("we \
are known for quality", "a quality leader", "committed to excellence") is NOT evidence of holding any \
particular certification, however confident it sounds — omit the entry rather than inferring a \
specific standard from vague quality marketing copy.
5. POSITIVE OBSERVATIONS ONLY. If the page denies, has stopped, or plans something, omit it \
entirely. Never return an entry meaning absence.
6. Do not infer one capability or fact from another.
7. Every entry needs a real quotation in "evidence". If you cannot quote the page, omit the entry.
8. Return an empty array if the page supports nothing. That is a correct answer for a contact page.

Known terms:
{term_list}
"""


def _build_system_prompt() -> str:
    from verification.capability_vocabulary import VOCABULARY

    lines = "\n".join(
        f"- {t.canonical}" + (f" (also called: {', '.join(t.aliases)})" if t.aliases else "")
        for t in VOCABULARY
    )
    return SYSTEM_PROMPT_TEMPLATE.format(term_list=lines)


@dataclass(frozen=True)
class CapabilityFinding:
    """One extracted, vocabulary-mapped observation.

    `canonical_term`/`category` are `None` when the model reported a
    real-sounding capability this vocabulary doesn't recognise yet —
    still returned, not dropped, so vocabulary gaps are visible rather
    than silently discarded (see `extract`'s own docstring).
    """

    reported_term: str
    canonical_term: Optional[str]
    category: Optional[str]
    relationship: str
    confidence: float
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


def _finding_from_assertion(assertion: dict, *, source_url: str) -> Optional[CapabilityFinding]:
    term_text = assertion.get("term")
    relationship = assertion.get("relationship")
    evidence = assertion.get("evidence")
    confidence = assertion.get("confidence")

    if not isinstance(term_text, str) or not term_text.strip():
        return None
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not 0.0 <= float(confidence) <= 1.0:
        return None
    if relationship == RELATIONSHIP_SOLD_ONLY:
        return None  # a trading observation, never a capability
    if relationship not in _VALID_RELATIONSHIPS:
        return None

    mapped: Optional[CapabilityTerm] = map_to_canonical(term_text)

    relationship_to_store = relationship
    if mapped and mapped.category == CATEGORY_STANDARD and relationship != RELATIONSHIP_ASSERTED:
        relationship_to_store = RELATIONSHIP_ASSERTED

    if mapped:
        required_markers = _EVIDENCE_MUST_CONTAIN.get(mapped.canonical)
        if required_markers is not None and not _evidence_has_required_marker(evidence, required_markers):
            return None  # the quoted evidence doesn't actually name the required marker -- reject, not just discount

    return CapabilityFinding(
        reported_term=term_text.strip(),
        canonical_term=mapped.canonical if mapped else None,
        category=mapped.category if mapped else None,
        relationship=relationship_to_store,
        confidence=float(confidence),
        evidence=evidence.strip(),
        source_url=source_url,
    )


class CapabilityExtractor:
    """Mirrors `ProductMatcher`/`FactoryPhotoVerifier`'s own
    injectable-client, never-raises design exactly: construction never
    requires an API key, only calling `extract` does, and any failure
    (network, parsing) returns an empty list with a logged error
    rather than propagating — a single supplier's bad page must never
    abort a batch run across thousands.
    """

    def __init__(self, client: Optional[Any] = None, model: str = DEFAULT_MODEL):
        self.model = model
        self._client = client
        self._system_prompt = _build_system_prompt()

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # imported lazily: optional dep for tests

            self._client = OpenAI()  # reads OPENAI_API_KEY from the environment
        return self._client

    def extract(self, page_text: str, *, source_url: str = "") -> List[CapabilityFinding]:
        """Extract capability findings from one page's text.
        Returns `[]` on a genuinely empty page (a valid outcome) and
        also on any failure to reach or parse the model's response —
        the two are not currently distinguished in the return value;
        check the logs if you need to tell them apart in bulk.
        """
        if not page_text or not page_text.strip():
            return []

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1500,
                temperature=0,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": f"Website page content:\n\n{page_text[:20_000]}"},
                ],
            )
            text = response.choices[0].message.content or ""
            assertions = _parse_model_json(text)
        except Exception as e:
            logger.error("Capability extraction failed for %s: %s", source_url or "<no url>", e)
            return []

        findings = []
        for assertion in assertions:
            finding = _finding_from_assertion(assertion, source_url=source_url)
            if finding is not None:
                findings.append(finding)
        return findings

    def extract_from_pages(self, pages: List[Any]) -> List[CapabilityFinding]:
        """Convenience wrapper over multiple
        `scrapers.own_website_scraper.OwnWebsitePage`-shaped objects
        (anything with `.url` and `.text`). Findings for the identical
        canonical term/relationship pair across two pages on the same
        site are not deduplicated here — `storage.repository`'s own
        insert is the deduplication point (see
        `add_capability_finding`'s docstring), so this stays a plain,
        stateless per-page map.
        """
        all_findings: List[CapabilityFinding] = []
        for page in pages:
            all_findings.extend(self.extract(page.text, source_url=page.url))
        return all_findings


# ═════════════════════════════════════════════════════════════
# Standalone assessor — see module docstring: NOT YET wired into
# ManufacturerVerifier.assess(). Same (verdict, explanation) shape as
# every function in verification/manufacturer_verifier.py, so wiring
# it in later is mechanical.
# ═════════════════════════════════════════════════════════════


def assess_own_website_capability(
    findings: List[CapabilityFinding],
) -> Tuple[Optional[bool], str]:
    """A supplier whose own website makes at least one credible
    in-house process/capability claim is real manufacturing-capability
    evidence — arguably stronger than most of ManufacturerVerifier's
    existing signals, since it's a specific, falsifiable claim rather
    than a category-level registry fact. A page describing capability
    only as subcontracted or sold-only is not itself evidence of
    trading (a genuine manufacturer legitimately subcontracts specific
    processes it doesn't run itself), so that case returns `None`
    ("no signal"), never `False` — this module should never be the
    reason a real manufacturer gets marked as a probable trader.

    `asserted` findings (market presence, OEM-supplier self-
    description) are deliberately invisible to this function -- "we
    serve the UK market" says nothing about whether a company
    manufactures anything, so they fall through to the same "no
    signal" default as an empty findings list, never treated as
    evidence either way.
    """
    if not findings:
        return None, "No capability findings from the supplier's own website (not yet assessed, or nothing extracted)"

    in_house = [f for f in findings if f.relationship == RELATIONSHIP_IN_HOUSE]
    if in_house:
        terms = ", ".join(sorted({f.canonical_term or f.reported_term for f in in_house}))
        return True, f"Own website describes in-house operation of: {terms}"

    subcontracted = [f for f in findings if f.relationship == RELATIONSHIP_SUBCONTRACTED]
    if subcontracted:
        terms = ", ".join(sorted({f.canonical_term or f.reported_term for f in subcontracted}))
        return None, f"Own website describes only subcontracted capability ({terms}) — not evidence either way"

    return None, "Capability findings present but none describe an in-house or subcontracted manufacturing relationship"


def unmapped_terms(findings: List[CapabilityFinding]) -> List[str]:
    """Terms the model reported that this vocabulary doesn't
    recognise yet — worth periodically reviewing and adding to
    `capability_vocabulary.VOCABULARY` rather than silently losing
    them.
    """
    return sorted({f.reported_term for f in findings if f.canonical_term is None})
