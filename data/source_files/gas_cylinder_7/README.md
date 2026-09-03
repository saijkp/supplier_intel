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
  2 genuine UK manufacturers found: RTN Ltd (trading subsidiary Lakeland
  Tankers Ltd) — own site: "RTN in partnership with our subsidiary company
  Lakeland Tankers Ltd... manufacture LPG tankers to the highest quality" —
  and Old Park Engineering Services Ltd — own site: "we design and
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

**Open item, not yet done**: unlike Material Handling's roster (see
`data/source_files/material_handling_14/README.md`), none of these 7 have
been run through Companies House verification yet
(`verification/uk_company_verification_service.py`) — the original sourcing
brief for this category called for that same UK-offices gate before
treating a candidate as fully confirmed, on top of the real-site
manufacture check already done here. `confirmed.csv` below reflects "real,
grounded, site-verified manufacturer" only, not "Companies House verified
UK company" — do that pass before relying on this roster the way Material
Handling's is relied on.

- `confirmed.csv` (7) — Company Name/Website, matching every other
  category roster's format. `resolve_confirmed_suppliers()` re-checks each
  against the live DB (still exists, still unflagged) rather than trusting
  this snapshot blindly, same as every other category.
