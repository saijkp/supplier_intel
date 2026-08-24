# Material Handling — confirmed suppliers

Directory name (`material_handling_14`) reflects the original snapshot size,
same convention as `injection_moulding_100` keeping its "100" long after its
own confirmed count diverged -- not a live count, see `confirmed.csv` itself
for the current one.

Roster for the Material Handling category, derived directly from the live
database rather than a manual audit (unlike `injection_moulding_100`, which
was a one-off ChatGPT-sourced candidate list). The original 14 (2026-08-20)
came in through the normal `discover()` pipeline (`discovery_source =
'discovery_service'`, ids 785-807) and were narrowed to "confirmed" using
this category's own gate from CLAUDE.md standing rule 9: Material Handling
requires UK offices, verified via Companies House
(`verification/uk_company_verification_service.py`), not by trusting the
site's own claimed address.

5 more (2026-08-21, ids 808-812: Truckmasters Handling, Commander Handling,
Feeler UK, KS Lift Trucks, Ashleyplant) were added after a live diagnostic
run surfaced them as real UK material-handling suppliers missing from the
original discovery batch -- created via `batch-upload`, then run through
`verify-uk-company` the same as the original 14 before being added here.
Same gate, same evidentiary bar, just a different entry path.

12 more (2026-08-21, ids 813-816/818-825) came from an externally-supplied
list of corrected domains for candidates originally marked dead/rejected --
re-run through the REAL gates before trusting the external claim, not taken
on faith: `discovery.candidate_validator.CandidateValidator.validate()`
(real fetch, LLM name extraction, name-match, product-term check) against
the corrected domain, then `match_company_name_against_companies_house()`
at the same 85-confidence bar `verify-uk-company` uses. Of 28 originally
supplied, 12 passed cleanly, 1 (Hitec Lift Trucks) turned out to have been
acquired-then-merged into Hiremech in 2025 (real redirect, not its own
company any more -- excluded, already covered by Hiremech's own entry), and
the remaining 15 either failed a real gate (dead domain, no product-term
match, genuine name mismatch) or were held for manual review (borderline
Companies House match, or a real registered company by the searched name
with no textual link from the site itself to that name) -- none of those
15 are in this roster. Three of the 12 (Windsor Materials Handling, PROMlift
UK, KDM Forklift Trucks) are stored under their verified Companies House
legal name (`canonical_name`) rather than their trading name, since that's
what the trading name resolves to on inspection (Windsor Engineering (Hull)
Ltd / PROMStahl UK / KDM Hire Ltd respectively) -- `confirmed.csv` below
still lists the trading name for readability, matched by domain regardless.
One duplicate was caught and fixed during this batch: the corrected domain
for "Commander Handling" (commanderhandling.com) created a second supplier
row (#817) for a company already on file at #809 (commanderhandling.co.uk,
same Companies House number) -- the dedup matcher didn't auto-merge on name
alone; #817 was flagged/retired per standing rule 8, #809 remains canonical.

- `confirmed.csv` (31) — suppliers with `companies_house_match_status =
  'verified' AND flagged = 0` at the time each was added to this roster.
  Company Name/Website only, matching the `injection_moulding_100` format;
  `resolve_confirmed_suppliers()` re-checks each against the live DB
  (still exists, still unflagged) rather than trusting this snapshot
  blindly, same as every other category.

No `excluded.csv` / `genuinely_dead.csv` / `name_mismatch.csv` /
`other_rejected.csv` — those categories came from `injection_moulding_100`'s
specific manual-audit workflow (a human triaging ChatGPT-sourced candidates
one by one) and have no equivalent here: Material Handling suppliers not in
this file either failed Companies House verification or are still
`companies_house_match_status` pending/unmatched, which is already visible
live on the supplier record rather than needing a static bucket file.
