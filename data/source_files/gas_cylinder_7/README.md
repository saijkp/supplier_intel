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
README.md`) — all 9 are `companies_house_match_status = 'verified'` at
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

**2 more added (2026-09-03), from a broader search pass** run specifically
to check whether real candidates existed outside BCGA/Liquid Gas UK's own
membership -- a real search pass (`gas cylinder manufacturer UK`,
`pressure vessel manufacturer UK`, `LPG tank manufacturer UK`) plus a
check of the **Pressure Vessel Manufacturers Forum (PVMF)**, a real, free,
UK trade association (sponsored/organised by LRQA, general pressure
equipment -- not gas-specific) with a public ~18-member list, same
"broader association, cylinders as one of several products" shape Metal
Pressing's own suppliers came from rather than one narrow tag:
- **Wefco (Gainsborough) Ltd** (`wefco.co.uk`) -- own site nav lists "LPG
  Tanks" as an explicit product line alongside Pressure Vessels and
  Hydrogen Vessels. On-category, not adjacent. Independently corroborated
  from both the search pass AND PVMF membership.
- **Tiverton Fabrications** (`tiverton-fabrications.co.uk`) -- "Custom
  Cylinder Manufacturer," own site: "cost effective steel fabricators of
  pressure vessels, bespoke cylinders, vacuum chambers, tubes & cryogenic
  vessels." Same tier as the BCGA finds (cylinder/vessel manufacturer, not
  propane-specific). A dedup candidate against an unrelated US company
  ("CMR Fabrications", `cmrfabrications.com`) fired on the shared word
  "Fabrications" alone (match score 0.76) -- manually reviewed and
  rejected (candidate #348): different domain, different country, no
  merge, same false-positive shape as Metal Pressing's Manor Tool/OEM
  Manufacturing checks.

**The other 16 PVMF members are real UK manufacturers, but a different
specific product** -- general industrial pressure vessels/process
equipment with no gas/LPG-cylinder specificity: CPE Pressure Vessels,
Abbott & Co (Newark), Barton Firtop Engineering, GFSA Ltd, Glapwell
Contracting Services, LBBC Beechwood, Portobello-RMF Engineering, QA Weld
Tech, Glacier Energy Services/Whiteley Read, plus several serving an
entirely different market -- Gilwood Ltd (now hygienic food-factory
equipment), Henry Technologies (refrigeration/AC), Spirotech Group
(refrigeration/chemical holding tanks), FlexEJ (heating expansion
vessels), KW Designed Solutions (subsea/nuclear test chambers), LTi
Metaltech (nuclear/defence/MRI-scanner cryostats -- "cryogenic" but
scientific, not gas storage), Therco-Serck (heat exchangers), TP Group Plc
(submarine life-support), Pfaudler-Balfour (glass-lined chemical
reactors). None are gas/LPG cylinder makers.

- `confirmed.csv` (9) — Company Name/Website, matching every other
  category roster's format. `resolve_confirmed_suppliers()` re-checks each
  against the live DB (still exists, still unflagged) rather than trusting
  this snapshot blindly, same as every other category.
