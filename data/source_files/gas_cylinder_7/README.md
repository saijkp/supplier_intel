# Gas Cylinder / Pressure Vessel Manufacturer — confirmed suppliers

UK-scoped category (2026-09-03): propane/gas cylinder and pressure-vessel
manufacturers, sourced entirely from two free structured trade-body
directories rather than paid search, same fetchability-first discipline
as every other source check this session.

**Sourcing:**
- British Compressed Gases Association (`bcga.co.uk`) "Services Directory"
  member list — 4 candidates (AMS Composite Cylinders, Chesterfield Special
  Cylinders, Luxfer Gas Cylinders, Wessington Cryogenics), each verified via
  direct site fetch before being added — real cylinder/pressure-vessel
  manufacturers, though mostly oxygen/medical/aerospace/industrial/
  cryogenic products rather than propane-specific.
- Liquid Gas UK (`liquidgasuk.org`, the UK LPG/bioLPG trade association,
  formerly UKLPG) member directory (`/about/our-members`) — full
  name-by-name pass across all ~106 real members (the site's own
  business-type filter is client-side/AJAX, not exercisable via a plain
  fetch, so every member was checked individually rather than filtered).
  2 genuine UK manufacturers found: Lakeland Tankers Ltd (hosted on parent
  company RTN Ltd's own site, rtnltd.co.uk) — own site: "RTN in partnership
  with our subsidiary company Lakeland Tankers Ltd... manufacture LPG
  tankers to the highest quality" — and Old Park Engineering Services Ltd
  — own site: "we design and
  manufacture a range of LPG vaporisers." A 3rd, OmegaFlex Ltd (brand:
  TracPipe) — "a BSI Kitemarked stainless steel semi-rigid gas piping
  system, manufactured in Oxfordshire since 2007" — was added after the
  same real Collect+Verify pass; category-adjacent (gas piping, not a
  cylinder/vessel itself) at the same tier Old Park Engineering's
  vaporisers were already accepted at.

**Explicitly excluded, not just skipped** (flagged shape, matching standing
rule 8's discipline even though these were caught before ever being
created as suppliers, so there's no DB row to flag):
- **Distributor/stockist, not manufacturer** — Pressure Vessel Services Ltd
  (`pvslimited.co.uk`): own site says "premier stockist... in collaboration
  with our trusted partners, we've crafted cutting-edge LPG Tanks" — a
  stockist working with manufacturing partners, not the manufacturer
  itself. Same Calor/Flogas-shaped distributor pattern the sourcing brief
  explicitly asked to be caught.
- **Service, not manufacturer** — Tankertech Ltd (installation/testing/
  repair/refurbishment), VTF Ltd ("UK's leading LPG vessel refurbisher"),
  South Staffs Industries Ltd (cylinder periodic testing/refurbishment).
- **Wrong industry entirely** — Proteus Equipment Ltd (a Liquid Gas UK
  affiliate member, but its own site is asphalt/road-surfacing equipment —
  hot boxes, manhole cover lifters — unrelated to gas), Marvtech Ltd
  (hotbox/truck-body manufacturer, same asphalt-industry shape), Monument
  Tools Ltd (hand tools), Trimetals Ltd (metal garden storage).
- **Real overseas manufacturer, but the UK-listed entity is a sales/
  distribution subsidiary with no UK-manufacture claim on its own site** —
  left out entirely, same reasoning as excluding Linde's non-UK network
  siblings from a UK-scoped category: Alfons Haar Ltd (parent: Alfons Haar
  Maschinenbau, Hamburg), ITO Europe Ltd (explicitly "the European
  subsidiary of ITO Group of Japan"), Elaflex Ltd (explicitly "UK
  Distributors" of the German Elaflex Group), Cavagna Group UK Ltd (global
  group site, "Excellence of the Made in Italy", no UK-production claim),
  Makeen Gas Equipment UK Limited (Danish parent, Makeen Energy).
- The remaining ~85 Liquid Gas UK members are plain LPG bulk/cylinder
  distributors, installers, trainers, consultants, or entirely unrelated
  businesses (leisure/travel operators, insurance, logistics, media) that
  happen to be trade-association members — not manufacturers, not
  disguised as any.

**Companies House verified (2026-09-03)**, same UK-offices gate Material
Handling's roster uses (see `data/source_files/material_handling_14/
README.md`) — all 7 are `companies_house_match_status = 'verified'` at
confidence >= 95, `companies_house_status = 'active'`, with a real
registered office on file. One, Lakeland Tankers Ltd, initially came back
`no_clear_match` (confidence 44) because the golden record had been named
"RTN Ltd (Lakeland Tankers)" -- a hybrid invented during sourcing, not the
company's real name -- and the parenthetical broke Companies House's
free-text search. Corrected `canonical_name` to `Lakeland Tankers Ltd`
(the actual Liquid Gas UK member name, and the entity that owns the site
content on rtnltd.co.uk, its parent company RTN Ltd's domain) and
re-verified: clean 100-confidence match to Lakeland Tankers Limited,
#02971298, active, Templeborough Depot, Sheffield Road, Sheffield --
same county as the site's own stated Barnsley/Hoyland address.

- `confirmed.csv` (7) — Company Name/Website, matching every other
  category roster's format. `resolve_confirmed_suppliers()` re-checks each
  against the live DB (still exists, still unflagged) rather than trusting
  this snapshot blindly, same as every other category.
