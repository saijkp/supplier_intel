# Material Handling — 14 confirmed suppliers

Roster for the Material Handling category, derived 2026-08-20 directly from
the live database rather than a manual audit (unlike
`injection_moulding_100`, which was a one-off ChatGPT-sourced candidate
list). These suppliers came in through the normal `discover()` pipeline
(`discovery_source = 'discovery_service'`, ids 785-807) and were narrowed
to "confirmed" using this category's own gate from CLAUDE.md standing rule
9: Material Handling requires UK offices, verified via Companies House
(`verification/uk_company_verification_service.py`), not by trusting the
site's own claimed address.

- `confirmed.csv` (14) — suppliers with `companies_house_match_status =
  'verified' AND flagged = 0` at the time this roster was generated.
  Company Name/Website only, matching the `injection_moulding_100` format;
  `resolve_confirmed_suppliers()` re-checks each against the live DB
  (still exists, still unflagged) rather than trusting this snapshot
  blindly, same as every other category.

No `excluded.csv` / `genuinely_dead.csv` / `name_mismatch.csv` /
`other_rejected.csv` — those categories came from `injection_moulding_100`'s
specific manual-audit workflow (a human triaging ChatGPT-sourced candidates
one by one) and have no equivalent here: the 9 Material Handling suppliers
not in this file either failed Companies House verification or are still
`companies_house_match_status` pending/unmatched, which is already visible
live on the supplier record rather than needing a static bucket file.
