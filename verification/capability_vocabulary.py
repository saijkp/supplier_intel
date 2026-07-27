"""
verification/capability_vocabulary.py

Controlled vocabulary mapping free-text manufacturing-capability
language (own-website copy, directory listing tags) onto a fixed set
of canonical terms — so "rotomoulding", "roto-moulding", and
"rotational molding" are recognised as one thing, not three.

Why exact matching, never substring/contains matching
--------------------------------------------------------
"We do NOT offer injection moulding" contains the substring "injection
moulding". A naive contains-check would assert the opposite of what
the sentence says. Whether a page *asserts* a capability is
`capability_extractor.CapabilityExtractor`'s job (it reads the whole
sentence); this module's only job is deciding which canonical term a
capability the extractor already decided to report corresponds to.
`map_to_canonical` therefore only ever does exact match on normalised
text — never a substring check.

Relationship to primary_categories / product_keywords / trailer_components
----------------------------------------------------------------------------
Those existing `suppliers` columns describe what a supplier *sells*
(products). This vocabulary describes how they *make* things
(processes/capabilities/standards) — a materially different question,
and the one that actually separates a rotomoulder from a trading
company that lists the same rotomoulded product. See
`capability_extractor`'s own module docstring for why that distinction
is the whole point of this addition.
"""

from __future__ import annotations

from typing import Dict, NamedTuple, Tuple

# Term categories — mirrors the shape ManufacturerVerifier's own
# signals already use (a short controlled set of strings), not a new
# enum type this project doesn't otherwise have a convention for.
CATEGORY_PROCESS = "process"
CATEGORY_CAPABILITY = "capability"
CATEGORY_STANDARD = "standard"
# Added for the commercial-intelligence extension (see
# verification/commercial_probability.py and DEPLOY.md's own note on
# this feature's design). Deliberately kept as three fine-grained
# categories rather than folding everything into CATEGORY_CAPABILITY:
# a buyer filtering "show me only logistics-relevant evidence" or
# "only OEM-readiness evidence" needs the category to actually
# distinguish these, matching the same reasoning CATEGORY_STANDARD was
# already kept separate from CATEGORY_CAPABILITY for.
CATEGORY_LOGISTICS = "logistics"
CATEGORY_MARKET_PRESENCE = "market_presence"
CATEGORY_OEM_READINESS = "oem_readiness"


class CapabilityTerm(NamedTuple):
    canonical: str
    category: str
    aliases: Tuple[str, ...] = ()


# Extend additively. Never rename an existing `canonical` value in
# place once suppliers have been assessed against it — canonical terms
# are stored verbatim in supplier_capabilities.canonical_term, so a
# rename would orphan every existing row's ability to be matched by
# find_suppliers_by_capability() rather than actually renaming them.
VOCABULARY: Tuple[CapabilityTerm, ...] = (
    # --- Polymer processing -------------------------------------------------
    CapabilityTerm("rotational moulding", CATEGORY_PROCESS, (
        "rotomoulding", "roto moulding", "roto-moulding", "rotomolding",
        "rotational molding", "rotocasting", "rotational casting",
    )),
    CapabilityTerm("injection moulding", CATEGORY_PROCESS, (
        "injection molding", "injection-moulding", "plastic injection moulding",
        "plastic injection molding",
    )),
    CapabilityTerm("blow moulding", CATEGORY_PROCESS, ("blow molding", "extrusion blow moulding", "ebm")),
    CapabilityTerm("thermoforming", CATEGORY_PROCESS, ("vacuum forming", "vacuum-forming", "pressure forming")),
    CapabilityTerm("compression moulding", CATEGORY_PROCESS, ("compression molding", "smc moulding", "dmc moulding")),
    CapabilityTerm("grp lamination", CATEGORY_PROCESS, (
        "grp", "frp", "fibreglass lamination", "fiberglass lamination",
        "hand layup", "hand lay-up", "resin transfer moulding", "rtm",
    )),
    CapabilityTerm("over-moulding", CATEGORY_PROCESS, (
        "overmoulding", "overmolding", "insert moulding", "insert molding", "two shot moulding",
    )),
    CapabilityTerm("plastic extrusion", CATEGORY_PROCESS, ("profile extrusion", "extruded profiles", "rubber extrusion")),

    # --- Metal forming and machining ----------------------------------------
    CapabilityTerm("press brake forming", CATEGORY_PROCESS, ("press braking", "sheet metal bending", "cnc bending")),
    CapabilityTerm("laser cutting", CATEGORY_PROCESS, ("fibre laser cutting", "fiber laser cutting", "laser profiling")),
    CapabilityTerm("tube bending", CATEGORY_PROCESS, ("mandrel bending", "cnc tube bending")),
    CapabilityTerm("stamping", CATEGORY_PROCESS, ("pressing", "progressive die stamping", "deep drawing")),
    CapabilityTerm("cnc machining", CATEGORY_PROCESS, ("cnc turning", "cnc milling", "5 axis machining", "precision machining")),
    CapabilityTerm("welding", CATEGORY_PROCESS, ("mig welding", "tig welding", "robotic welding", "spot welding", "fabrication")),
    CapabilityTerm("die casting", CATEGORY_PROCESS, ("pressure die casting", "aluminium die casting", "aluminum die casting")),
    CapabilityTerm("forging", CATEGORY_PROCESS, ("hot forging", "cold forging", "drop forging")),

    # --- Finishing ------------------------------------------------------------
    CapabilityTerm("hot dip galvanising", CATEGORY_PROCESS, ("hot dip galvanizing", "galvanising", "galvanizing", "hdg")),
    CapabilityTerm("powder coating", CATEGORY_PROCESS, ("powder coat", "powdercoating")),
    CapabilityTerm("electroplating", CATEGORY_PROCESS, ("zinc plating", "chrome plating", "e-coat", "electrocoating")),
    CapabilityTerm("anodising", CATEGORY_PROCESS, ("anodizing", "hard anodising", "hard anodizing")),

    # --- Assembly and electrical -----------------------------------------------
    CapabilityTerm("sub-assembly", CATEGORY_CAPABILITY, (
        "subassembly", "sub assembly", "contract assembly", "kitting",
        "final assembly", "component assembly",
    )),
    CapabilityTerm("wiring harness assembly", CATEGORY_CAPABILITY, (
        "wire harness assembly", "cable harness", "loom assembly", "wiring loom", "cable assembly",
    )),
    CapabilityTerm("pcb assembly", CATEGORY_CAPABILITY, ("smt assembly", "pcba", "electronics assembly")),

    # --- Tooling and engineering capability ---------------------------------
    CapabilityTerm("in-house tool room", CATEGORY_CAPABILITY, (
        "in house toolroom", "tool room", "toolroom", "mould making", "mold making", "tool making",
    )),
    CapabilityTerm("design for manufacture", CATEGORY_CAPABILITY, ("dfm", "design support", "engineering support")),
    CapabilityTerm("bespoke low volume manufacture", CATEGORY_CAPABILITY, (
        "bespoke manufacturing", "custom manufacturing", "low volume production", "made to order",
    )),

    # --- Standards --------------------------------------------------------------
    CapabilityTerm("e-mark approval", CATEGORY_STANDARD, ("e mark", "emark", "e-marked", "ece type approval")),
    CapabilityTerm("ece r10", CATEGORY_STANDARD, ("e-mark emc", "emc approval", "r10")),
    CapabilityTerm("ece r48", CATEGORY_STANDARD, ("r48", "lighting installation approval")),
    CapabilityTerm("iso 9001", CATEGORY_STANDARD, ("iso9001", "iso 9001:2015")),
    CapabilityTerm("iatf 16949", CATEGORY_STANDARD, ("iatf16949", "ts 16949", "ts16949")),
    CapabilityTerm("iso 14001", CATEGORY_STANDARD, ("iso14001",)),
    CapabilityTerm("ce marking", CATEGORY_STANDARD, ("ce mark", "ce marked", "ce certified", "ce compliance")),

    # --- Logistics (commercial intelligence extension) --------------------
    # Evidence source is the supplier's own shipping/incoterms page, not
    # a directory listing -- same capability_extractor pipeline every
    # process/capability term already goes through. "relationship" here
    # means: in_house = they operate this themselves (own fleet, own
    # bonded warehouse), subcontracted = via a named or implied logistics
    # partner. A bare capability claim with no such distinction stated
    # uses "asserted" (see verification.capability_extractor's own note
    # on why a third relationship value was added).
    CapabilityTerm("ddp shipping", CATEGORY_LOGISTICS, (
        "ddp", "delivered duty paid", "ddp shipping available", "ddp incoterms",
    )),
    CapabilityTerm("dap shipping", CATEGORY_LOGISTICS, ("dap", "delivered at place")),
    CapabilityTerm("fob shipping", CATEGORY_LOGISTICS, ("fob", "free on board", "fob only")),
    CapabilityTerm("cif shipping", CATEGORY_LOGISTICS, ("cif", "cost insurance and freight", "cost, insurance and freight")),
    CapabilityTerm("door to door shipping", CATEGORY_LOGISTICS, ("door-to-door delivery", "door to door delivery")),
    CapabilityTerm("uk warehouse", CATEGORY_LOGISTICS, (
        "warehouse in the uk", "uk based warehouse", "uk stock", "uk distribution centre", "uk distribution center",
    )),
    CapabilityTerm("eu warehouse", CATEGORY_LOGISTICS, (
        "warehouse in europe", "european warehouse", "eu stock", "eu distribution centre", "eu distribution center",
    )),
    CapabilityTerm("customs expertise", CATEGORY_LOGISTICS, (
        "customs clearance", "customs brokerage", "in house customs", "customs handling",
    )),

    # --- Market presence (commercial intelligence extension) ---------------
    # Website-derived evidence of which markets a supplier already
    # serves. Complements, rather than replaces, the two signals already
    # on the supplier record: exports_to_uk/eu/us (self-reported "main
    # markets" text, via BaseNormalizer.infer_export_flags_from_markets)
    # and confirmed_shipments_uk/eu/us (actual customs/trade data, via
    # the Volza normalizer -- the strongest evidence tier of the three).
    # This is the third, own-website-text tier. "relationship" is always
    # "asserted" for these -- there's no in-house-vs-subcontracted
    # version of "we serve the UK market."
    CapabilityTerm("serves uk market", CATEGORY_MARKET_PRESENCE, (
        "uk customers", "supplying the uk", "serving the uk market", "uk clients",
    )),
    CapabilityTerm("serves eu market", CATEGORY_MARKET_PRESENCE, (
        "european customers", "supplying europe", "serving the european market", "eu clients",
    )),
    CapabilityTerm("serves north america market", CATEGORY_MARKET_PRESENCE, (
        "north american customers", "supplying north america", "us customers", "usa customers", "canadian customers",
    )),
    CapabilityTerm("serves australia market", CATEGORY_MARKET_PRESENCE, (
        "australian customers", "supplying australia", "serving the australian market",
    )),
    CapabilityTerm("oem supplier", CATEGORY_MARKET_PRESENCE, (
        "oem experience", "original equipment manufacturer supplier", "tier 1 supplier", "tier one supplier",
    )),

    # --- OEM readiness (commercial intelligence extension) ------------------
    CapabilityTerm("ppap capability", CATEGORY_OEM_READINESS, (
        "ppap", "production part approval process", "ppap submission",
    )),
    CapabilityTerm("cad engineering support", CATEGORY_OEM_READINESS, (
        "cad support", "cad design", "3d cad", "engineering drawings support",
    )),
    CapabilityTerm("traceability system", CATEGORY_OEM_READINESS, (
        "full traceability", "batch traceability", "lot traceability", "material traceability",
    )),
)


def normalise_term(text: str) -> str:
    """Lowercase, collapse whitespace, strip characters that only ever
    vary typographically ("roto-moulding" vs "roto moulding" is one
    term, not two). Deliberately named `normalise` (not `normalize`)
    to match this project's own British-English convention elsewhere
    (`normalise_company_name` in deduplication/name_utils.py).
    """
    lowered = text.strip().lower()
    for ch in ("-", ".", ",", "/", "(", ")"):
        lowered = lowered.replace(ch, " ")
    return " ".join(lowered.split())


def _build_alias_index() -> Dict[str, CapabilityTerm]:
    index: Dict[str, CapabilityTerm] = {}
    for term in VOCABULARY:
        for surface_form in (term.canonical, *term.aliases):
            key = normalise_term(surface_form)
            existing = index.get(key)
            if existing is not None and existing.canonical != term.canonical:
                raise ValueError(
                    f"surface form {surface_form!r} is claimed by both "
                    f"{existing.canonical!r} and {term.canonical!r} — fix VOCABULARY"
                )
            index[key] = term
    return index


ALIAS_INDEX: Dict[str, CapabilityTerm] = _build_alias_index()


def map_to_canonical(text: str) -> CapabilityTerm | None:
    """The canonical `CapabilityTerm` for free-text `text`, or `None`
    if this vocabulary doesn't recognise it. Exact match only — see
    module docstring for why.
    """
    return ALIAS_INDEX.get(normalise_term(text))
