# Mudguard Suppliers — confirmed suppliers

Registered in `CATEGORY_ROSTERS` on 2026-09-03, formalizing a real sourcing
pass done earlier this session that had never been checked in as a roster
(see [[project_injection_moulding_llm_source_closed]]'s own mention of this
investigation, which shares its Automechanika tags with the separate
Mudguard Suppliers work). Pure formalization of already-collected evidence
-- no new sourcing, no new cost.

**Sourcing**: Automechanika's already-imported exhibitor data (676 rows,
129 product-group tags), 3 relevant tags -- `Fenders / mudguards`,
`Plastic exterior body panels`, `RV/caravan body repairs, plastic`. A
plain `product=mudguard` API/CLI search only matches the first, narrowest
tag (11 of the 33) -- the other 22 carry `Plastic exterior body panels`
(mudguards are functionally plastic exterior body panels) or, for one
row (Combicar SRL), `RV/caravan body repairs, plastic`. **A search for
just the word "mudguard" will under-report this roster's real size** --
checked and confirmed directly against the live DB, not assumed.

**Reconciliation done before this roster was created** (2026-09-03):
an uncommitted `mudguard_suppliers_tracker.csv` (33 rows, real
Collect+Verify data -- addresses, phones, emails, source pages) had been
sitting in the working tree unregistered. Cross-checked every row against
the live database by domain, and separately queried the DB for every
supplier carrying any of the 3 tags above, to confirm nothing had drifted
since the tracker was generated and nothing relevant was missing from it:
- All 33 tracker rows matched a real, unflagged supplier row exactly by
  domain -- no stale/renamed/dead entries.
- The DB's 3-tag query found exactly 3 more matches than the tracker,
  and all 3 are correctly excluded for real reasons, not an oversight:
  BRS Plast Oto Aksesuar A.S. and Danyang Mincheng Auto Parts Co., Ltd.
  have no domain on file at all (same "didn't make it in" shape as
  Metal Pressing's own no-domain exclusions), and Mercedes-Benz Used
  Parts & Services GmbH is already `flagged=1` in the database (an
  OEM used-parts reseller, not a mudguard manufacturer -- excluded
  before this reconciliation, not by it).

Net: the tracker was complete and accurate as-is. `confirmed.csv` below
is a direct Company Name/Website extraction from it, same format as
every other category roster.

- `confirmed.csv` (33) — Company Name/Website, matching every other
  category roster's format. `resolve_confirmed_suppliers()` re-checks
  each against the live DB (still exists, still unflagged) rather than
  trusting this snapshot blindly, same as every other category.

**Not yet done**: no Companies House verification (this category has no
UK-only scope requirement the way Material Handling/Gas Cylinder do, so
that gate doesn't automatically apply here -- most of these 33 are
non-UK manufacturers by address). No product_keywords normalization --
each supplier keeps whichever of the 3 real Automechanika tags it was
actually sourced under, not rewritten to a single shared "mudguard" term.
