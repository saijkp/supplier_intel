"""
main.py

CLI entry point for the Supplier Intelligence Platform.

Usage:
    python main.py init-db                     Create the database and all tables
    python main.py status                      Show schema version and row counts
    python main.py doctor                      Check config / API keys / DB health
    python main.py run "LED marker light"      Run the full scrape->dedup->verify->score pipeline
    python main.py verify                      Re-run Qichacha verification only
    python main.py rescore [--all]              Re-run scoring (only unscored suppliers, or every one with --all)
    python main.py certs                       Check for expiring/malformed certifications
    python main.py coverage --gaps-only         BOM category coverage vs shortlist targets
    python main.py search "wheel bearings" --require "e-mark approval" --manufacturers-only
                                            The full procurement search: product + required certifications + verified-manufacturer filter, one call
    python main.py find-websites               Find a domain for suppliers whose listing didn't give one
    python main.py extract-capabilities        Crawl suppliers' own websites for manufacturing capability evidence
    python main.py find-by-capability "X"      Find suppliers with capability evidence for X, e.g. "rotational moulding"
    python main.py list                        List suppliers (filterable)
    python main.py report                      Generate a Markdown supplier report
    python main.py export-csv                  Export suppliers to CSV
    python main.py export-excel                 Export suppliers to Excel (.xlsx), with contact/address enrichment columns
    python main.py discover "trailer axle" --country China   AI-assisted supplier discovery, grounded in real search results
    python main.py discover --product "jockey wheel" --source llm --limit 100   Same, but candidates come from an LLM's own knowledge instead of SerpAPI
    python main.py collect --supplier-id 123     Visit a supplier's website with a real headless browser (collection.SiteCollector)
    python main.py batch-upload companies.csv    Enrich a spreadsheet of companies through the same single-company enrichment path
    python main.py verify-ai --supplier-id 123   AI cross-check + confidence score against existing verification signals
    python main.py reverify --supplier-id 123    Re-collect then re-verify an already-known supplier
    python main.py set-verdict --supplier-id 123 --criterion A --value Pass   Set one Audit-tab verdict from the CLI
    python main.py correct-supplier 123 --clear-domain   Fix a bad record (e.g. a false-match domain): clear it, re-resolve via the real pipeline, log the change
"""

from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from config.settings import (
    DB_PATH,
    configure_logging,
    missing_api_keys,
    SCORING_WEIGHTS,
    TRAILER_HS_CODES,
)
from storage.database import initialise_schema, get_schema_version, table_counts
from storage.repository import SupplierRepository

console = Console()


@click.group()
def cli() -> None:
    """Supplier Intelligence Platform — trailer components sourcing."""
    configure_logging()


@cli.command("init-db")
def init_db() -> None:
    """Create the SQLite database and all tables/indexes if missing."""
    initialise_schema()
    version = get_schema_version()
    console.print(f"[green]OK[/green] Database initialised at [bold]{DB_PATH}[/bold]")
    console.print(f"[green]OK[/green] Schema version: [bold]{version}[/bold]")


# Moved to batch/category_roster.py (CATEGORY_ROSTERS) so api/app.py's
# Audit endpoints can share the exact same category->roster mapping and
# confirmed/excluded resolution logic, not a second independently-drifting
# copy of it.


@cli.command("status")
@click.option("--category", default=None,
              help="Live status for one audited candidate category (e.g. \"injection moulding\") "
                   "instead of table row counts -- confirmed/excluded are re-checked against the "
                   "live DB (supplier still exists / suppliers.flagged), dead/rejected/mismatch are "
                   "read from the checked-in roster at data/source_files/<category>/ (those buckets "
                   "have no DB representation at all -- a rejected candidate never became a supplier "
                   "row, see that directory's own README). No file written, no scraping/API calls -- "
                   "just SQLite + small CSV reads, fast enough to run any time.")
def status(category: Optional[str]) -> None:
    """Show schema version and current row counts for every table, or
    (with --category) live confirmed/excluded/dead/rejected/mismatch
    counts for one audited candidate category."""
    if not DB_PATH.exists():
        console.print(
            f"[yellow]No database found at {DB_PATH}. "
            f"Run `python main.py init-db` first.[/yellow]"
        )
        sys.exit(1)

    if category:
        _status_for_category(category)
        return

    version = get_schema_version()
    counts = table_counts()

    console.print(f"Database: [bold]{DB_PATH}[/bold]")
    console.print(f"Schema version: [bold]{version}[/bold]\n")

    table = Table(title="Table Row Counts")
    table.add_column("Table", style="cyan")
    table.add_column("Rows", justify="right", style="magenta")
    for name, count in counts.items():
        table.add_row(name, str(count))
    console.print(table)


def _status_for_category(category: str) -> None:
    from deduplication.domain_utils import extract_domain
    from batch.category_roster import CATEGORY_ROSTERS, read_roster_csv, resolve_confirmed_suppliers, roster_dir_for_category

    roster_dir = roster_dir_for_category(category)
    if roster_dir is None:
        supported = ", ".join(repr(k) for k in CATEGORY_ROSTERS) or "(none yet)"
        console.print(f"[yellow]No roster checked in for --category {category!r}. Supported: {supported}[/yellow]")
        return

    repo = SupplierRepository()

    confirmed_resolution = resolve_confirmed_suppliers(category, repo)
    confirmed_now = len(confirmed_resolution["suppliers"])
    drifted_to_excluded = confirmed_resolution["drifted_to_excluded"]
    missing = confirmed_resolution["missing"]

    excluded_roster = read_roster_csv(roster_dir, "excluded.csv")
    excluded_now = 0
    drifted_to_confirmed = []
    for r in excluded_roster:
        domain = extract_domain(r.get("Website") or "")
        supplier = repo.find_by_domain(domain) if domain else None
        if supplier is not None and not supplier.get("flagged"):
            drifted_to_confirmed.append(r["Company Name"])
        else:
            excluded_now += 1

    dead_count = len(read_roster_csv(roster_dir, "genuinely_dead.csv"))
    mismatch_count = len(read_roster_csv(roster_dir, "name_mismatch.csv"))
    rejected_count = len(read_roster_csv(roster_dir, "other_rejected.csv"))

    total = confirmed_now + excluded_now + dead_count + mismatch_count + rejected_count + len(missing)

    console.print(f"[bold]{category}[/bold] -- live status (confirmed/excluded checked against the DB just now)\n")

    table = Table(title=f"{category} -- candidate status")
    table.add_column("Status", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("Confirmed", str(confirmed_now))
    table.add_row("Excluded (flagged in DB)", str(excluded_now))
    table.add_row("Genuinely dead", str(dead_count))
    table.add_row("Other rejected", str(rejected_count))
    table.add_row("Name mismatch", str(mismatch_count))
    table.add_row("Total", str(total))
    console.print(table)

    if drifted_to_excluded:
        console.print(f"\n[yellow]! {len(drifted_to_excluded)} roster \"confirmed\" candidate(s) are now flagged in the DB (moved to Excluded above):[/yellow]")
        for name in drifted_to_excluded:
            console.print(f"    - {name}")
    if drifted_to_confirmed:
        console.print(f"\n[yellow]! {len(drifted_to_confirmed)} roster \"excluded\" candidate(s) are no longer flagged in the DB (moved to Confirmed above):[/yellow]")
        for name in drifted_to_confirmed:
            console.print(f"    - {name}")
    if missing:
        console.print(f"\n[red]! {len(missing)} roster \"confirmed\" candidate(s) no longer resolve to any supplier in the DB (excluded from Total above -- investigate):[/red]")
        for name in missing:
            console.print(f"    - {name}")
    if not (drifted_to_excluded or drifted_to_confirmed or missing):
        console.print("\n[green]No drift from the checked-in roster -- confirmed/excluded match the DB exactly.[/green]")


@cli.command("doctor")
@click.option("--live", is_flag=True,
              help="Go beyond checking that API keys are present -- make a minimal real call to "
                   "every configured integration and report whether it actually works. Spends a "
                   "small amount of real quota/money per configured key. Run this after deploying "
                   "(e.g. to Railway) or whenever credentials change.")
def doctor(live: bool) -> None:
    """Sanity-check configuration: API keys, HS codes, scoring weights,
    and whether the database has been initialised."""
    console.print("[bold]Supplier Intelligence Platform — Setup Check[/bold]\n")

    missing = missing_api_keys()
    if missing:
        console.print(f"[yellow]! Missing API keys (needed for live scraping): {', '.join(missing)}[/yellow]")
        console.print("  Set these in a .env file at the project root.\n")
    else:
        console.print("[green]OK[/green] All required API keys are set.\n")

    weight_sum = round(sum(SCORING_WEIGHTS.values()), 6)
    console.print(f"[green]OK[/green] Scoring weights sum to {weight_sum} ({SCORING_WEIGHTS})")
    console.print(f"[green]OK[/green] Tracking {len(TRAILER_HS_CODES)} HS codes for trailer components")

    if DB_PATH.exists():
        console.print(f"[green]OK[/green] Database exists at {DB_PATH} (schema v{get_schema_version()})")
    else:
        console.print(f"[yellow]! Database not yet created. Run `python main.py init-db`.[/yellow]")

    if not live:
        console.print("\n[dim]Run with --live to make a real call to every configured "
                       "integration and confirm it actually works.[/dim]")
        return

    console.print("\n[bold]Live checks[/bold] (this spends a small amount of real quota/money)\n")
    from diagnostics.live_environment_check import run_all_checks

    results = run_all_checks()
    table = Table()
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    table.add_column("Time", justify="right")
    status_style = {"pass": "[green]PASS[/green]", "fail": "[red]FAIL[/red]", "skipped": "[dim]skipped[/dim]"}
    for r in results:
        table.add_row(r.name, status_style.get(r.status, r.status), r.detail, f"{r.duration_ms}ms")
    console.print(table)

    failed = [r for r in results if r.status == "fail"]
    passed = [r for r in results if r.status == "pass"]
    skipped = [r for r in results if r.status == "skipped"]
    console.print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped (not configured).")
    if failed:
        console.print(
            "[red]Modify or reconfigure the failed integrations above before relying on them "
            "in a real run.[/red]"
        )
        sys.exit(1)


@cli.command("run")
@click.argument("query")
@click.option("--source", "-s", "sources", multiple=True,
              help="Limit to specific source(s), e.g. -s alibaba -s hktdc. Default: all configured sources.")
@click.option("--no-verify", is_flag=True, help="Skip the Qichacha verification stage.")
@click.option("--no-score", is_flag=True, help="Skip the scoring stage.")
@click.option("--find-websites", is_flag=True,
              help="For suppliers with no known website, search their company name (Google/SerpAPI) "
                   "and validate the top result before accepting it as their domain. Runs before "
                   "--extract-capabilities so a newly-found domain is picked up in the same run. "
                   "Off by default: costs a paid SerpAPI call per domain-less supplier.")
@click.option("--extract-capabilities", is_flag=True,
              help="Also crawl each supplier's own website for manufacturing capability evidence, "
                   "contact details, and factory photos. Off by default: real HTTP requests plus an "
                   "OpenAI call per page and per photo, a different cost/traffic profile than the "
                   "rest of a run.")
@click.option("--verify-facilities", is_flag=True,
              help="Confirm each supplier's claimed address resolves to a real place (Google Places "
                   "outside China, Amap within it). Off by default: a paid API call per supplier "
                   "with an address on file.")
@click.option("--check-linkedin", is_flag=True,
              help="Confirm whether each supplier has a findable LinkedIn company page. Off by "
                   "default: a paid SerpAPI call per supplier.")
@click.option("--limit", type=int, default=None,
              help="Cap raw results kept PER SOURCE to this many -- for a small, cost-controlled "
                   "test run against a source you haven't exercised against real credentials yet. "
                   "Passed through as each source's own natural count parameter where one exists "
                   "(max_results for alibaba/indiamart/google; max_pages=1 for everything "
                   "page-based, so a test run never fetches more than one page in the first place) "
                   "AND enforced as a hard cap afterward regardless -- see "
                   "SupplierIntelligencePipeline.run's own note on why both. No cap by default, "
                   "matching every prior version of this command's behaviour.")
def run(query: str, sources: tuple, no_verify: bool, no_score: bool, find_websites: bool,
        extract_capabilities: bool, verify_facilities: bool, check_linkedin: bool,
        limit: Optional[int]) -> None:
    """Run the full pipeline for QUERY: scrape -> dedup/merge -> verify -> score."""
    from pipeline.orchestrator import SupplierIntelligencePipeline, build_limit_scraper_kwargs

    pipeline = SupplierIntelligencePipeline()
    scraper_kwargs = build_limit_scraper_kwargs(limit, list(sources) or None, list(pipeline.scrapers.keys()))
    stats = pipeline.run(
        query,
        sources=list(sources) or None,
        scraper_kwargs=scraper_kwargs,
        run_verification=not no_verify,
        run_scoring=not no_score,
        run_website_discovery=find_websites,
        run_capability_extraction=extract_capabilities,
        run_facility_verification=verify_facilities,
        run_linkedin_check=check_linkedin,
        results_limit=limit,
    )

    console.print(f"[bold]Pipeline run: \"{query}\"[/bold]\n")
    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    for key in (
        "scraped", "scrape_errors", "normalised", "skipped_no_name",
        "created", "merged", "review_queued", "shipment_records",
        "verified", "manufacturer_assessed", "website_discovered",
        "capability_extracted", "contact_emails_added", "contact_phones_added",
        "contact_forms_recorded", "photos_assessed", "facility_address_verified",
        "linkedin_checked", "scored",
    ):
        table.add_row(key.replace("_", " "), str(stats.get(key, 0)))
    console.print(table)


@cli.command("verify")
def verify() -> None:
    """Re-run Qichacha verification for any unverified Chinese suppliers already on file."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_verification_only()
    console.print(f"[green]OK[/green] Verified {stats['verified']} supplier(s).")


@cli.command("rescore")
@click.option("--all", "rescore_all", is_flag=True,
              help="Re-score every supplier, not just never-scored ones -- needed after "
                   "changing SCORING_WEIGHTS or verification/scorer.py's logic itself.")
def rescore(rescore_all: bool) -> None:
    """Re-run scoring for any currently-unscored suppliers (or every supplier with --all)."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_full_rescore() if rescore_all else pipeline.run_scoring_only()
    console.print(f"[green]OK[/green] Scored {stats['scored']} supplier(s).")


@cli.command("assess-manufacturers")
@click.option("--force", is_flag=True, help="Re-assess every supplier, not just never-assessed ones.")
def assess_manufacturers(force: bool) -> None:
    """Re-run manufacturer verification (business scope, registered capital,
    tenure consistency, certifications) for suppliers needing assessment."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_manufacturer_assessment_only(force=force)
    console.print(f"[green]OK[/green] Assessed {stats['manufacturer_assessed']} supplier(s).")


@cli.command("import-automechanika")
@click.argument("path", type=click.Path(exists=True))
@click.option("--include-tier2", is_flag=True,
              help='Also import the "Tier-2 (Non-Trailer)" sheet -- real companies, kept for '
                   "later expansion per the workbook's own README, but not trailer-relevant "
                   "today. Off by default.")
def import_automechanika(path: str, include_tier2: bool) -> None:
    """Import the Automechanika exhibitor export (Core Suppliers + Extended - Review
    sheets by default). Goes through the exact same dedup/merge logic as a live
    scrape -- a company already in the database from Alibaba/HKTDC/etc. merges
    rather than duplicates. See normalizers/automechanika_normalizer.py for the
    exact column mapping this assumes."""
    from deduplication.matcher import SupplierMatcher
    from normalizers.automechanika_normalizer import (
        AutomechanikaNormaliser,
        read_automechanika_workbook,
    )
    from pipeline.static_list_import import import_static_supplier_list

    console.print(f"Reading {path}...")
    raw_rows = read_automechanika_workbook(path, include_tier2=include_tier2)
    console.print(f"{len(raw_rows)} rows found. Importing...")

    repo = SupplierRepository()
    matcher = SupplierMatcher(repo)
    stats = import_static_supplier_list(
        repo, matcher, raw_rows,
        source_label="automechanika_2026", normaliser=AutomechanikaNormaliser(),
    )

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    for key, value in stats.as_dict().items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)

    if stats.review_queued:
        console.print(
            f"\n[yellow]{stats.review_queued} possible duplicate(s) queued for review — "
            f"run `python main.py review list` to see them.[/yellow]"
        )


@cli.command("find-websites")
@click.option("--force", is_flag=True,
              help="Re-attempt every domain-less supplier, not just ones never searched.")
@click.option("--limit", default=1000, show_default=True,
              help="Stop after this many suppliers. Each one costs a paid SerpAPI call, so start "
                   "small on a first run.")
def find_websites(force: bool, limit: int) -> None:
    """For suppliers with a company name but no known website (common for Alibaba/
    IndiaMart/Made-in-China listings that don't expose one), search the name and
    validate the top result before accepting it as their domain. Costs a paid
    SerpAPI call per supplier searched — see the module docstring in
    scrapers/company_website_finder.py for why the validation step exists and
    isn't optional."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_website_discovery_only(force=force, limit=limit)
    console.print(f"[green]OK[/green] Found and validated {stats['website_discovered']} new website(s).")


@cli.command("verify-facilities")
@click.option("--force", is_flag=True, help="Re-check every supplier with an address, not just new ones.")
@click.option("--limit", default=1000, show_default=True,
              help="Stop after this many suppliers. Each one costs a paid Google Places/Amap "
                   "call, so start small on a first run.")
def verify_facilities(force: bool, limit: int) -> None:
    """Confirm each supplier's claimed address resolves to a real place. Routes
    China to Amap, everywhere else to Google Places — see the module docstring in
    verification/facility_address_verifier.py for why, and for the real
    registration friction to expect on the China path specifically. Photo
    verification runs separately, as part of "extract-capabilities" — it reuses
    that command's own website fetch rather than needing its own."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_facility_verification_only(force=force, limit=limit)
    console.print(f"[green]OK[/green] Verified {stats['facility_address_verified']} address(es) as real places.")


@cli.command("discover")
@click.argument("product_arg", metavar="PRODUCT", required=False)
@click.option("--product", "product_opt", default=None,
              help="The product to discover manufacturers for. Same as the positional PRODUCT "
                   "argument -- accepted as an option too so --source llm's documented invocation "
                   "(--product \"jockey wheel\" --source llm --limit 100) works as written.")
@click.option("--category", default=None, help="Optional product category, recorded on discovery_runs.")
@click.option("--country", default=None, help="Optional country to qualify the search (e.g. China).")
@click.option(
    "--source", type=click.Choice(["serpapi", "llm", "1688", "companies_house_sic"]),
    default="serpapi", show_default=True,
    help="serpapi (default): candidates from real SerpAPI search hits. llm: candidates "
         "gpt-4o-mini proposes from its own knowledge (discovery/llm_candidate_source.py), "
         "costing OpenAI calls instead of SerpAPI ones -- still gated by the exact same "
         "real-fetch/content-match validation before anything is stored, see that module's "
         "docstring for why this doesn't weaken the anti-hallucination guarantee. 1688: "
         "China1688Scraper (Apify-backed, real cost per product) -- a Mandarin search term "
         "is expected (pass the term itself, this CLI does not translate it). Currently "
         "diagnostic-only: fetches real "
         "listings and writes them to raw_source_data as evidence, but does NOT validate "
         "or create supplier rows yet -- see DiscoveryService._discover_1688's own "
         "docstring for why (1688 gives no independent company-website field to validate "
         "against, only marketplace-hosted URLs). companies_house_sic: bulk UK Companies "
         "House SIC-code search (--sic-codes, required with this source) -- free existence/"
         "registration lookup, but each real company still costs one SerpAPI search "
         "(finding a website CH doesn't provide) plus a real fetch+OpenAI validation call "
         "for any that resolve, same real per-candidate cost as serpapi/llm. UK-only -- only "
         "makes sense for a genuinely UK-scoped category. See "
         "discovery/companies_house_sic_source.py's own docstring for why this source's "
         "candidates skip the soft-signal (but not the hard self-declaration) trader gate.")
@click.option(
    "--sic-codes", default=None,
    help="Comma-separated UK SIC 2007 codes (e.g. \"28220,46140,46690,77120,77320,77390,"
         "33170,33200\" for Material Handling) -- required with --source companies_house_sic, "
         "ignored otherwise. ORed together in one real Companies House search. See "
         "discovery/companies_house_sic_source.py's own docstring for why a category's real "
         "SIC footprint is usually broader than its single \"cleanest\" manufacturing code.")
@click.option(
    "--sic-name-keywords", default=None,
    help="Comma-separated keywords (e.g. \"forklift,lift truck,handling,plant\") -- a free "
         "pre-filter on the company name Companies House itself returned, before any paid "
         "SerpAPI/OpenAI call. Only meaningful with --source companies_house_sic. See "
         "discovery/companies_house_sic_source.py's find_candidates docstring for the real, "
         "quantified false-negative tradeoff (checked against this codebase's own confirmed "
         "suppliers) before using this on a new category -- a pure brand name or generic "
         "business name with no product-category word in it will never pass this filter, "
         "no matter how the keyword list is tuned.")
@click.option("--limit", "max_candidates", default=20, show_default=True,
              help="Stop after this many candidate companies. Each one costs a paid SerpAPI "
                   "search (--source serpapi) or an OpenAI call (--source llm) plus, for "
                   "candidates that pass initial filtering, a real HTTP fetch and an OpenAI "
                   "validation call either way -- start small on a first run.")
@click.option("--domain-bias", default=None,
              help="Optional TLD suffix (e.g. \".co.uk\") to add one extra site:-restricted query "
                   "variant, biasing SerpAPI results toward that domain -- a cheap first-pass filter "
                   "for a country-scoped run, since --country only qualifies the query text, it "
                   "never restricts results to that country. Additive, not a replacement for the "
                   "unbiased variants (a real target can sit on a plain .com too) -- see "
                   "discovery/query_builder.py's build_queries docstring.")
@click.option("--require-uk-registration", is_flag=True, default=False,
              help="Reject a candidate with no confirmed ACTIVE UK Companies House registration "
                   "before the paid OpenAI validation call, not after -- the UK-office gate for "
                   "categories like Material Handling, moved earlier in the pipeline so a candidate "
                   "Companies House would reject anyway never costs an OpenAI call. Free API, "
                   "matches on a cheap guess at the company name from the raw search-result title "
                   "(coarser than the canonical_name match main.py verify-uk-company does later, "
                   "which remains the authoritative check) -- see "
                   "discovery/candidate_validator.py's \"Gate 3.5\" docstring.")
@click.option("--role-words", default=None,
              help="Comma-separated extra role words (e.g. \"dealer,distributor,stockist\") to add "
                   "one quoted-phrase query variant each, ON TOP OF the default manufacturer/"
                   "supplier/factory templates -- a directory-style query shape for categories where "
                   "the real target is an established dealer/distributor, not a raw manufacturer, "
                   "see discovery/query_builder.py's build_queries docstring for why this stays "
                   "opt-in rather than a global template change.")
@click.option("--recover-dead-domains", is_flag=True, default=False,
              help="Opt-in: when a candidate fails validation specifically because its domain is "
                   "dead/unreachable (not a marketplace, trader, or name-mismatch rejection -- "
                   "those stay as-is), search the company name once more and validate the top "
                   "result through the SAME real gate -- zero special trust for a recovered "
                   "candidate. Real extra SerpAPI+fetch+OpenAI cost per dead candidate.")
def discover(
    product_arg: Optional[str], product_opt: Optional[str], category: Optional[str],
    country: Optional[str], source: str, sic_codes: Optional[str], sic_name_keywords: Optional[str],
    max_candidates: int, domain_bias: Optional[str], require_uk_registration: bool,
    role_words: Optional[str], recover_dead_domains: bool,
) -> None:
    """AI-assisted supplier discovery. Every accepted supplier traces to a
    real fetched website and that website's own text corroborating the
    identity and product -- see discovery/discovery_service.py's module
    docstring for the full anti-hallucination pipeline, for both
    --source serpapi (grounded in real search hits) and --source llm
    (grounded the same way, just proposed by the model instead of a
    search engine). Rediscovering an existing supplier merges via the
    same dedup engine main.py run already uses, never duplicates."""
    from discovery.discovery_service import DiscoveryService

    product = product_opt or product_arg
    if not product:
        console.print("[yellow]Give a product, either as PRODUCT or --product.[/yellow]")
        return

    sic_codes_list = [c.strip() for c in sic_codes.split(",") if c.strip()] if sic_codes else None
    if source == "companies_house_sic" and not sic_codes_list:
        console.print("[yellow]--source companies_house_sic requires --sic-codes.[/yellow]")
        return
    sic_name_keywords_list = (
        [k.strip() for k in sic_name_keywords.split(",") if k.strip()] if sic_name_keywords else None
    )

    companies_house_client = None
    if require_uk_registration:
        from verification.companies_house_client import CompaniesHouseClient

        companies_house_client = CompaniesHouseClient()

    role_words_list = [w.strip() for w in role_words.split(",") if w.strip()] if role_words else None

    service = DiscoveryService(companies_house_client=companies_house_client)
    outcome = service.discover(
        product, category=category, country=country, max_candidates=max_candidates, source=source,
        domain_tld_bias=domain_bias, extra_role_words=role_words_list,
        recover_dead_domains=recover_dead_domains, sic_codes=sic_codes_list,
        sic_name_keywords=sic_name_keywords_list,
    )

    if source == "1688":
        import json as _json

        console.print(
            f"[bold]{len(outcome.raw_1688_listings)}[/bold] listing(s) fetched from 1688 "
            f"(Apify), written to raw_source_data as evidence. Diagnostic-only: nothing "
            f"validated or inserted into suppliers yet -- see --source 1688's own --help text.\n"
        )
        for i, listing in enumerate(outcome.raw_1688_listings, 1):
            console.print(f"[cyan]--- listing {i} ---[/cyan]")
            console.print(_json.dumps(listing, ensure_ascii=False, indent=2))
        return

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    if source == "llm":
        table.add_row("generated", str(outcome.candidates_generated))
        table.add_row("website resolved", str(outcome.website_resolved))
        table.add_row("content matched", str(outcome.content_matched))
        table.add_row("deduplicated (merged into existing supplier)", str(outcome.candidates_duplicate))
        table.add_row("inserted (new suppliers created)", str(len(outcome.new_supplier_ids)))
        table.add_row("  (queued for dedup review)", str(len(outcome.review_queued_supplier_ids)))
        table.add_row("rejected", str(outcome.candidates_rejected))
    elif source == "companies_house_sic":
        table.add_row("Companies House SIC hits", str(outcome.candidates_generated))
        table.add_row("website found for", str(outcome.candidates_found))
        table.add_row("website resolved", str(outcome.website_resolved))
        table.add_row("content matched", str(outcome.content_matched))
        table.add_row("deduplicated (merged into existing supplier)", str(outcome.candidates_duplicate))
        table.add_row("inserted (new suppliers created)", str(len(outcome.new_supplier_ids)))
        table.add_row("  (queued for dedup review)", str(len(outcome.review_queued_supplier_ids)))
        table.add_row("rejected", str(outcome.candidates_rejected))
    else:
        table.add_row("candidates found", str(outcome.candidates_found))
        table.add_row("candidates validated", str(outcome.candidates_validated))
        table.add_row("candidates rejected", str(outcome.candidates_rejected))
        table.add_row("merged into existing supplier", str(outcome.candidates_duplicate))
        table.add_row("new suppliers created", str(len(outcome.new_supplier_ids)))
        table.add_row("  (queued for dedup review)", str(len(outcome.review_queued_supplier_ids)))
    console.print(table)


@cli.command("import-linde-dealers")
@click.option("--limit", default=None, type=int,
              help="Cap how many dealers to process (small-test-first, then the full "
                   "~370). Applied after fetching Linde's real list, so a small test "
                   "still sees a representative real slice.")
def import_linde_dealers(limit: Optional[int]) -> None:
    """Import Linde Material Handling's own published worldwide authorized-dealer
    network (a real, static JSON file Linde serves directly -- no search interaction,
    no per-dealer API calls needed). Bypasses the trader/product-term validation gate
    entirely -- Linde's own network membership is stronger identity evidence than
    either gate approximates -- but each listed website gets one real, lightweight
    liveness check before entering the roster (not a full validate() pass). Goes
    through the same dedup/merge engine every other source uses -- a dealer already on
    file from a live search merges rather than duplicates. See
    discovery/linde_dealer_import.py for the full design rationale."""
    from deduplication.matcher import SupplierMatcher
    from discovery.linde_dealer_import import import_linde_dealer_network

    repo = SupplierRepository()
    matcher = SupplierMatcher(repo)
    console.print("Fetching Linde's real dealer network...")
    stats = import_linde_dealer_network(repo, matcher, limit=limit)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("total dealers", str(stats.total_dealers))
    table.add_row("no website listed (imported anyway)", str(stats.no_website))
    table.add_row("linde-owned domain (imported without a domain)", str(stats.linde_owned_domain))
    table.add_row("website live", str(stats.website_live))
    table.add_row("website dead (skipped)", str(stats.website_dead))
    if stats.static_import:
        for key, value in stats.static_import.as_dict().items():
            table.add_row(key.replace("_", " "), str(value))
    console.print(table)


@cli.command("import-fabtech-exhibitors")
@click.option("--limit", default=None, type=int,
              help="Cap how many exhibitors to process (small-test-first, then the full "
                   "~1,400). Applied after fetching FABTECH's real exhibitor list, so a "
                   "small test still sees a representative real slice.")
def import_fabtech_exhibitors_cmd(limit: Optional[int]) -> None:
    """Import FABTECH's own real exhibitor directory (a server-rendered A2Z Inc-powered
    list -- no login/JS/pagination needed). For each exhibitor, fetches their own
    SmallWorldLabs profile page for a real company website, liveness-checks it, and
    imports survivors through the same dedup/merge engine every other source uses.
    Bypasses the trader/product-term validation gate entirely -- exhibiting at a real
    trade show under one's own profile is itself a real identity signal -- but each
    listed website still gets one real, lightweight liveness check before entering the
    roster. See discovery/fabtech_exhibitor_import.py for the full design rationale."""
    from deduplication.matcher import SupplierMatcher
    from discovery.fabtech_exhibitor_import import import_fabtech_exhibitors

    repo = SupplierRepository()
    matcher = SupplierMatcher(repo)
    console.print("Fetching FABTECH's real exhibitor directory...")
    stats = import_fabtech_exhibitors(repo, matcher, limit=limit)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("total exhibitors", str(stats.total_exhibitors))
    table.add_row("test/QA accounts excluded", str(stats.test_accounts_excluded))
    table.add_row("no website listed (imported anyway)", str(stats.no_website))
    table.add_row("website live", str(stats.website_live))
    table.add_row("website blocked (imported, tagged unconfirmed)", str(stats.website_blocked))
    table.add_row("website dead (skipped)", str(stats.website_dead))
    if stats.static_import:
        for key, value in stats.static_import.as_dict().items():
            table.add_row(key.replace("_", " "), str(value))
    console.print(table)


@cli.command("backfill-discovery-keywords")
def backfill_discovery_keywords() -> None:
    """One-off repair: suppliers `discover` created before it started
    recording product_keywords are invisible to `search`'s product-term
    matching unless their own name happens to contain the term.
    Reconstructs the missing value from pipeline_jobs history -- see
    discovery/discovery_service.py's backfill_product_keywords()
    docstring for exactly how. Fills gaps only, safe to run more than
    once."""
    from discovery.discovery_service import DiscoveryService

    service = DiscoveryService()
    result = service.backfill_product_keywords()

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("product_keywords backfilled", str(len(result["updated_supplier_ids"])))
    table.add_row("already had product_keywords", str(len(result["already_had_keywords_supplier_ids"])))
    table.add_row("referenced supplier no longer exists", str(len(result["missing_supplier_ids"])))
    console.print(table)
    if result["updated_supplier_ids"]:
        console.print(f"Backfilled supplier ids: {result['updated_supplier_ids']}")


@cli.command("export-discovered")
@click.argument("product")
@click.option("--output", "-o", default=None,
              help="Write the CSV here (default: discovered_<product-slug>.csv in the current directory).")
@click.option("--discovery-source", default="discovery_service", show_default=True,
              help="Only export suppliers whose discovery_source matches this -- the default is "
                   "what discover() always sets, regardless of --source serpapi/llm.")
def export_discovered(product: str, output: Optional[str], discovery_source: str) -> None:
    """Export every supplier `discover PRODUCT` has ever created -- across
    however many separate runs it took -- as a minimal Company Name /
    Website CSV, ready to feed straight into `batch-upload`. discover()
    itself only ever sets name/domain/country, never address or contact
    details -- this is the bridge to batch-upload's fuller enrichment
    pipeline (see discovery/discovery_service.py's
    export_for_batch_upload() docstring for why the bridge is needed at
    all, and why this re-queries persistent supplier state rather than
    a single run's transient output)."""
    from discovery.discovery_service import DiscoveryService

    service = DiscoveryService()
    path, count = service.export_for_batch_upload(product, output_path=output, discovery_source=discovery_source)
    if count:
        console.print(f"[green]OK[/green] Exported {count} supplier(s) to [bold]{path}[/bold]")
    else:
        console.print(
            f"[yellow]No suppliers found for product={product!r}, discovery_source={discovery_source!r}.[/yellow] "
            f"Wrote a header-only CSV to [bold]{path}[/bold] anyway."
        )


@cli.command("collect")
@click.option("--supplier-id", type=int, default=None,
              help="Collect against one specific supplier (ignores --pending/--limit/--force).")
@click.option("--pending", is_flag=True,
              help="Batch mode: collect against every supplier needing it (has a domain, "
                   "never collected before).")
@click.option("--limit", default=20, show_default=True,
              help="Stop after this many suppliers in --pending mode. Each one launches a "
                   "real headless-browser session, so start small on a first run.")
@click.option("--force", is_flag=True,
              help="In --pending mode, re-collect every supplier with a domain, not just "
                   "ones never collected.")
@click.option("--default-region", default=None,
              help="ISO 3166-1 alpha-2 fallback (e.g. GB) for phone-number parsing when a "
                   "supplier's own country isn't set yet -- always true for a freshly-created "
                   "supplier, since a national-format phone number (no +44 prefix) can't be "
                   "recognised without a region hint. Only use this when you KNOW the batch is "
                   "regionally scoped (e.g. a UK-only category) -- never overrides a real, "
                   "known country.")
def collect(supplier_id: Optional[int], pending: bool, limit: int, force: bool,
            default_region: Optional[str]) -> None:
    """Visit supplier website(s) with a real headless browser (collection.
    SiteCollector) and save HTML/screenshots/extracted contact+product data.
    Either --supplier-id ONE or --pending a batch -- see collection/
    collection_service.py's module docstring for the concurrency/timeout
    safeguards a --pending batch runs under."""
    from collection.collection_service import CollectionService

    if not supplier_id and not pending:
        console.print("[red]X[/red] Specify either --supplier-id or --pending.")
        return

    service = CollectionService(default_region_fallback=default_region)
    if supplier_id:
        outcome = service.collect(supplier_id)
        console.print(f"[green]OK[/green] Supplier #{supplier_id}: {outcome['status']} "
                       f"({outcome['pages_visited']} page(s) visited)"
                       + (f" -- {outcome['error']}" if outcome.get("error") else ""))
    else:
        stats = service.collect_pending(limit=limit, force=force)
        table = Table(show_header=False)
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="magenta")
        for key in ("attempted", "succeeded", "failed", "total_eligible", "status"):
            table.add_row(key.replace("_", " "), str(stats.get(key, "")))
        console.print(table)


@cli.command("batch-upload")
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--output", "-o", default=None,
              help="Write the results CSV here once done (default: <csv_path> with a "
                   "_results suffix, next to the input file).")
@click.option("--plain", is_flag=True, default=False,
              help="Write primary_phone as a plain value instead of an Excel-safe formula "
                   "string (default: Excel-safe, since phone numbers otherwise open as "
                   "scientific notation in Excel). Use --plain for a clean, machine-readable CSV.")
@click.option("--search-reputation", is_flag=True, default=False,
              help="Opt-in criterion-D evidence gathering: 3 real SerpAPI searches per row "
                   "('[name] scam', '[name] review', '[name] factory tour'), snippets only -- "
                   "never a Clean/Flagged judgment. Off by default: real per-row SerpAPI cost "
                   "on top of the collection cost every row already incurs.")
@click.option("--recover-dead-domains", is_flag=True, default=False,
              help="Opt-in: when a row's domain turns out to be dead/unreachable, search the "
                   "company name once more and validate the top result through the SAME real "
                   "candidate_validator gate any fresh discovery candidate goes through -- zero "
                   "special trust for a recovered row. Requires --recovery-product-term. Real "
                   "extra SerpAPI+fetch+OpenAI cost per dead row.")
@click.option("--recovery-product-term", default=None,
              help="Required when --recover-dead-domains is set -- the product/category term "
                   "candidate_validator's product-term gate checks a recovered candidate's page "
                   "against (a CSV row has no product concept of its own to fall back on).")
@click.option("--default-region", default=None,
              help="ISO 3166-1 alpha-2 fallback (e.g. GB) for phone-number parsing when a "
                   "supplier's own country isn't set yet -- always true for a freshly-created "
                   "supplier, since a national-format phone number (no +44 prefix) can't be "
                   "recognised without a region hint, and collect() always runs before this "
                   "command's own address/country extraction step. Same option as "
                   "main.py collect --default-region -- only use this when you KNOW the batch "
                   "is regionally scoped (e.g. a UK-only category) -- never overrides a real, "
                   "known country. Found live: a real 71-row batch went from 18 to 59 rows with "
                   "a phone number once this was set, versus every other phone-extraction fix "
                   "combined.")
def batch_upload(
    csv_path: str, output: Optional[str], plain: bool, search_reputation: bool,
    recover_dead_domains: bool, recovery_product_term: Optional[str], default_region: Optional[str],
) -> None:
    """Enrich a spreadsheet of companies through the exact same
    single-company enrichment path collect/discover already use --
    SupplierMatcher.resolve_and_store() then CollectionService.collect()
    per row, no second extraction pipeline (see batch/batch_service.py's
    own module docstring for exactly how each row is handled, including
    the needs_url/domain-derived-placeholder-name cases). Company name
    and/or website columns are detected by fuzzy header matching --
    "Company", "Company Name", "Website", "URL" and similar all work.
    Each row costs a real headless-browser visit -- a 50-200 row
    spreadsheet will take a while, start small on a first run."""
    import dataclasses
    import uuid
    from pathlib import Path

    from batch.batch_service import BatchService
    from batch.csv_exporter import flatten_batch_results
    from batch.csv_parser import parse_csv

    if recover_dead_domains and not recovery_product_term:
        console.print("[red]X[/red] --recover-dead-domains requires --recovery-product-term.")
        return

    with open(csv_path, "rb") as f:
        csv_bytes = f.read()
    parsed = parse_csv(csv_bytes)
    if not parsed.rows:
        console.print("[yellow]No rows found in this CSV (or no recognisable header).[/yellow]")
        return

    console.print(
        f"[bold]{len(parsed.rows)}[/bold] row(s). Detected columns: "
        f"company name = [cyan]{parsed.company_name_column or '(none found)'}[/cyan], "
        f"website = [cyan]{parsed.website_column or '(none found)'}[/cyan]"
    )
    if parsed.duplicate_row_indices:
        console.print(f"[dim]{len(parsed.duplicate_row_indices)} duplicate row(s) detected (exact name+website repeat).[/dim]")

    repo = SupplierRepository()
    job_id = str(uuid.uuid4())
    repo.create_pipeline_job(job_id=job_id, query=f"[batch] {csv_path}", options={"filename": csv_path})
    repo.mark_pipeline_job_running(job_id)

    if search_reputation:
        console.print(
            f"[yellow]--search-reputation enabled: 3 real SerpAPI searches per row "
            f"({len(parsed.rows)} row(s) -> up to {len(parsed.rows) * 3} searches).[/yellow]"
        )
    if recover_dead_domains:
        console.print(
            f"[yellow]--recover-dead-domains enabled: up to 1 extra SerpAPI search + 2 fetch/"
            f"validation attempts per row whose domain turns out dead (up to {len(parsed.rows)} "
            f"row(s) could trigger this).[/yellow]"
        )

    service = BatchService(repo=repo, default_region_fallback=default_region)
    outcome = service.run_batch(
        parsed.rows, batch_job_id=job_id, search_reputation=search_reputation,
        recover_dead_domains=recover_dead_domains, recovery_product_term=recovery_product_term,
    )
    repo.mark_pipeline_job_completed(job_id, stats=dataclasses.asdict(outcome))

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("total rows", str(outcome.total_rows))
    table.add_row("needs url", str(outcome.needs_url))
    table.add_row("processed", str(outcome.processed))
    table.add_row("succeeded", str(outcome.succeeded))
    table.add_row("failed", str(outcome.failed))
    table.add_row("placeholder names used", str(outcome.placeholder_names_used))
    table.add_row("placeholder names replaced", str(outcome.placeholder_names_replaced))
    table.add_row("placeholder names rejected (junk/parking page)", str(outcome.placeholder_names_rejected))
    table.add_row("placeholder names conflicting (not applied)", str(outcome.placeholder_names_conflicting))
    table.add_row("addresses found", str(outcome.addresses_found))
    table.add_row("addresses conflicting (not applied)", str(outcome.addresses_conflicting))
    table.add_row("factory locations found", str(outcome.factory_locations_found))
    table.add_row("factory locations conflicting (not applied)", str(outcome.factory_locations_conflicting))
    table.add_row("rows with new facility photo candidates", str(outcome.facility_photos_found))
    if search_reputation:
        table.add_row("rows with reputation snippets found", str(outcome.reputation_snippets_found))
    if recover_dead_domains:
        table.add_row("dead domains recovered", str(outcome.domains_recovered))
    console.print(table)

    rows = repo.get_batch_upload_rows(job_id)
    csv_text = flatten_batch_results(rows, repo=repo, excel_safe_phone=not plain)
    output_path = output or f"{Path(csv_path).with_suffix('')}_results.csv"
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(csv_text)
    console.print(f"[green]OK[/green] Results written to [bold]{output_path}[/bold]")


@cli.command("export-tracker")
@click.argument("confirmed_csv", type=click.Path(exists=True))
@click.option("--universe-csv", type=click.Path(exists=True), default=None,
              help="Optional broader candidate list (Company Name/Website columns, same "
                   "shape as CONFIRMED_CSV) to scope the Removed Candidates tab -- e.g. "
                   "every candidate ever considered for this sourcing pass, not just the "
                   "ones still confirmed. Defaults to CONFIRMED_CSV itself, which means "
                   "Removed Candidates will come back empty unless a flagged supplier is "
                   "somehow also still in the confirmed list (it shouldn't be).")
@click.option("--output", "-o", default=None,
              help="Main tracker CSV path (default: <confirmed_csv> with a _tracker suffix).")
@click.option("--removed-output", default=None,
              help="Removed-candidates CSV path (default: <confirmed_csv> with a "
                   "_removed_candidates suffix).")
@click.option("--xlsx-output", default=None,
              help="Optional: also write a single .xlsx workbook to this path, three tabs "
                   "(Supplier Audit / Qualified / Removed Candidates) matching a buyer's own "
                   "reference tracker shape -- built from the exact same data as the CSV "
                   "outputs above (see batch/tracker_exporter.py's build_tracker_workbook), "
                   "which are written unchanged either way. Off by default.")
def export_tracker(
    confirmed_csv: str, universe_csv: Optional[str], output: Optional[str],
    removed_output: Optional[str], xlsx_output: Optional[str],
) -> None:
    """Group 3: export a confirmed candidate list (Company Name/Website
    columns, e.g. the output of validating a candidate list through
    the same gate discover() uses) into the buyer's own procurement-
    tracker CSV format -- see batch/tracker_exporter.py's own
    docstring for the exact column layout and why A-D/Qualified always
    come back blank. Every row must already have a matching supplier
    in the database (by domain) -- run `batch-upload` on the CSV first
    if it hasn't been enriched yet."""
    from pathlib import Path

    from batch.csv_parser import parse_csv
    from batch.tracker_exporter import (
        build_removed_candidates_export,
        build_tracker_export,
        build_tracker_workbook,
    )
    from deduplication.domain_utils import extract_domain

    def _resolve_ids(csv_path: str) -> List[int]:
        with open(csv_path, "rb") as f:
            parsed = parse_csv(f.read())
        ids: List[int] = []
        for row in parsed.rows:
            domain = extract_domain(row.website or "")
            supplier = repo.find_by_domain(domain) if domain else None
            if supplier is None:
                console.print(f"[yellow]! No supplier found for {row.website!r} -- skipped (run batch-upload first?).[/yellow]")
                continue
            ids.append(supplier["id"])
        return ids

    repo = SupplierRepository()
    confirmed_ids = _resolve_ids(confirmed_csv)
    universe_ids = _resolve_ids(universe_csv) if universe_csv else confirmed_ids

    tracker_csv = build_tracker_export(confirmed_ids, repo)
    removed_csv = build_removed_candidates_export(universe_ids, repo)

    output_path = output or f"{Path(confirmed_csv).with_suffix('')}_tracker.csv"
    removed_output_path = removed_output or f"{Path(confirmed_csv).with_suffix('')}_removed_candidates.csv"
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(tracker_csv)
    with open(removed_output_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(removed_csv)

    console.print(f"[green]OK[/green] {len(confirmed_ids)} confirmed candidate(s) written to [bold]{output_path}[/bold]")
    console.print(f"[green]OK[/green] Removed-candidates list written to [bold]{removed_output_path}[/bold]")

    if xlsx_output:
        workbook_bytes = build_tracker_workbook(confirmed_ids, universe_ids, repo)
        with open(xlsx_output, "wb") as f:
            f.write(workbook_bytes)
        console.print(f"[green]OK[/green] 3-tab workbook (Supplier Audit / Qualified / Removed Candidates) written to [bold]{xlsx_output}[/bold]")


@cli.command("verify-uk-company")
@click.argument("csv_path", type=click.Path(exists=True), required=False)
@click.option("--supplier-id", type=int, default=None, help="Verify one specific supplier by id instead of a CSV.")
@click.option("--force", is_flag=True, default=False,
              help="Re-check suppliers already checked (default: skip anyone with "
                   "companies_house_checked_at already set).")
def verify_uk_company(csv_path: Optional[str], supplier_id: Optional[int], force: bool) -> None:
    """UK Companies House verification -- the UK-office validation gate
    for categories that require it (e.g. Material Handling). CLI-only,
    opt-in, and deliberately has NO category awareness of its own (see
    verification/uk_company_verification_service.py's own docstring):
    run this manually against whatever candidate list or supplier id
    needs checking, same as batch-upload and the discovery pipeline.

    Pass either CSV_PATH (Company Name/Website columns, resolved to
    suppliers by domain -- same shape as batch-upload/export-tracker)
    or --supplier-id, not both. Every row must already have a matching
    supplier in the database.

    A name that doesn't cleanly match anything in Companies House is
    NEVER auto-rejected -- it's recorded as "no_clear_match" (check
    manually), since a trading name differing from the registered
    legal name is common and not itself suspicious."""
    from batch.csv_parser import parse_csv
    from deduplication.domain_utils import extract_domain
    from verification.uk_company_verification_service import UKCompanyVerificationService

    if bool(csv_path) == bool(supplier_id):
        console.print("[red]X[/red] Specify exactly one of CSV_PATH or --supplier-id.")
        return

    repo = SupplierRepository()
    service = UKCompanyVerificationService(repo=repo)

    if supplier_id:
        outcome = service.verify_uk_company(supplier_id)
        confidence_note = f" (confidence={outcome['confidence']})" if outcome.get("confidence") is not None else ""
        console.print(f"[green]OK[/green] Supplier #{supplier_id}: {outcome['match_status']}{confidence_note}")
        return

    with open(csv_path, "rb") as f:
        parsed = parse_csv(f.read())
    ids: List[int] = []
    for row in parsed.rows:
        domain = extract_domain(row.website or "")
        supplier = repo.find_by_domain(domain) if domain else None
        if supplier is None:
            console.print(f"[yellow]! No supplier found for {row.website!r} -- skipped (run batch-upload first?).[/yellow]")
            continue
        ids.append(supplier["id"])

    console.print(f"[bold]{len(ids)}[/bold] supplier(s) resolved from {csv_path}.")
    outcome = service.verify_uk_company_batch(ids, force=force)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    for key in ("attempted", "verified", "inactive", "no_clear_match", "total_given", "status"):
        table.add_row(key.replace("_", " "), str(outcome.get(key, "")))
    console.print(table)


@cli.command("verify-ai")
@click.option("--supplier-id", type=int, default=None,
              help="Verify one specific supplier (ignores --pending/--limit/--force).")
@click.option("--pending", is_flag=True,
              help="Batch mode: verify every supplier needing it (never AI-assessed before).")
@click.option("--limit", default=20, show_default=True,
              help="Stop after this many suppliers in --pending mode. Each one costs a real "
                   "OpenAI call, so start small on a first run.")
@click.option("--force", is_flag=True,
              help="In --pending mode, re-verify every supplier, not just ones never assessed.")
def verify_ai(supplier_id: Optional[int], pending: bool, limit: int, force: bool) -> None:
    """Cross-check a supplier's data against existing verification signals
    (manufacturer assessment, facility address, LinkedIn presence, phone/
    certification consistency), assign a 0-100 ai_confidence_score
    (deliberately separate from composite_score -- see verification_ai/
    confidence_scorer.py), and generate an AI summary/strengths/risks/
    suitable-customer-types. Either --supplier-id ONE or --pending a batch."""
    from verification_ai.verification_service import VerificationService

    if not supplier_id and not pending:
        console.print("[red]X[/red] Specify either --supplier-id or --pending.")
        return

    service = VerificationService()
    if supplier_id:
        outcome = service.verify(supplier_id)
        console.print(f"[green]OK[/green] Supplier #{supplier_id}: confidence {outcome['confidence_score']}/100 "
                       f"({outcome['verdict']}), narrative_generated={outcome['narrative_generated']}")
        if outcome["inconsistencies"]:
            console.print("[yellow]Inconsistencies found:[/yellow]")
            for inconsistency in outcome["inconsistencies"]:
                console.print(f"  - {inconsistency}")
    else:
        stats = service.verify_pending(limit=limit, force=force)
        table = Table(show_header=False)
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="magenta")
        for key in ("attempted", "succeeded", "failed", "total_eligible"):
            table.add_row(key.replace("_", " "), str(stats.get(key, "")))
        console.print(table)


@cli.command("reverify")
@click.option("--supplier-id", type=int, default=None, help="Reverify one specific supplier.")
@click.option("--older-than-days", type=int, default=None,
              help="Batch mode: reverify every supplier last verified more than this many days "
                   "ago (or never verified at all).")
@click.option("--limit", default=20, show_default=True,
              help="Stop after this many suppliers in --older-than-days mode.")
def reverify(supplier_id: Optional[int], older_than_days: Optional[int], limit: int) -> None:
    """Re-collect (real headless browser) then re-verify (AI cross-check) an
    already-known supplier -- the "update existing records when reverified"
    workflow. Genuine changes are appended to the supplier's change log, not
    silently overwritten -- see `main.py history` to inspect it."""
    from storage.repository import SupplierRepository
    from verification_ai.verification_service import VerificationService

    if not supplier_id and older_than_days is None:
        console.print("[red]X[/red] Specify either --supplier-id or --older-than-days.")
        return

    service = VerificationService()
    if supplier_id:
        outcome = service.reverify(supplier_id)
        console.print(f"[green]OK[/green] Supplier #{supplier_id}: collection={outcome['collection']['status']}, "
                       f"confidence={outcome['verification']['confidence_score']}/100")
        return

    repo = SupplierRepository()
    candidates = repo.get_suppliers_needing_reverification(older_than_days=older_than_days, limit=limit)
    console.print(f"[bold]Reverifying {len(candidates)} supplier(s) last verified more than "
                   f"{older_than_days} day(s) ago (or never)...[/bold]")
    succeeded = 0
    for supplier in candidates:
        try:
            service.reverify(supplier["id"])
            succeeded += 1
        except Exception as e:
            console.print(f"[red]X[/red] Supplier #{supplier['id']}: {e}")
    console.print(f"[green]OK[/green] Reverified {succeeded}/{len(candidates)} supplier(s).")


@cli.command("history")
@click.option("--supplier-id", type=int, required=True)
def history(supplier_id: int) -> None:
    """Show verification history and field-change log for one supplier --
    the audit trail behind ai_confidence_score/ai_summary and every other
    field verification_ai/collection writes."""
    from storage.repository import SupplierRepository

    repo = SupplierRepository()
    verification_rows = repo.get_verification_history(supplier_id)
    change_rows = repo.get_supplier_change_log(supplier_id)

    console.print(f"[bold]Verification history for supplier #{supplier_id}[/bold] ({len(verification_rows)} run(s))")
    for row in verification_rows:
        console.print(f"  {row['run_at']}  [{row['verification_type']}]  "
                       f"confidence={row['confidence_score']}  verdict={row['verdict']}")

    console.print(f"\n[bold]Change log[/bold] ({len(change_rows)} change(s))")
    for row in change_rows:
        console.print(f"  {row['changed_at']}  {row['field_name']}: "
                       f"{row['old_value']!r} -> {row['new_value']!r}  (by {row['changed_by']})")


@cli.command("correct-supplier")
@click.argument("supplier_id", type=int)
@click.option("--clear-domain", is_flag=True,
              help="Clear a wrong domain/website (logged as its own supplier_change_log entry, "
                   "changed_by='manual'), then re-resolve it through the exact same real "
                   "search+validation path find-websites/POST /companies/enrich already use "
                   "(scrapers.company_website_finder.CompanyWebsiteFinder), and re-collect from "
                   "the corrected site if one validates. Costs one paid SerpAPI call, plus a "
                   "small OpenAI call if a candidate site is found and needs the grounded-name "
                   "corroboration check. Wrong tool if the ORIGINAL search term (company name) "
                   "was itself wrong -- use --set-domain instead.")
@click.option("--set-domain", default=None,
              help="Write an ALREADY-human-verified domain directly, no search -- for when the "
                   "original search term was itself wrong (a fresh search under the same wrong "
                   "name just re-surfaces the same wrong candidate, or nothing). Combine with "
                   "--set-name to correct the stored name too.")
@click.option("--set-name", default=None,
              help="Only used with --set-domain -- corrects canonical_name too, when the "
                   "original name (not just the domain) was wrong.")
@click.option("--flag-duplicate", default=None,
              help="Mark this record excluded (flagged, never deleted) instead of correcting it "
                   "-- for a bad record that turns out to be a duplicate of an already-existing "
                   "real supplier (e.g. --set-domain reported 'domain_conflict'). Pass the reason "
                   "as the value.")
@click.option("--reason", default=None,
              help="Why this correction is being made -- recorded on supplier_change_log. "
                   "Defaults to a generic note naming the cleared/old value if omitted.")
def correct_supplier(
    supplier_id: int, clear_domain: bool, set_domain: Optional[str], set_name: Optional[str],
    flag_duplicate: Optional[str], reason: Optional[str],
) -> None:
    """Reusable fix for a bad supplier record -- e.g. a false-match domain a validation
    gap let through (see scrapers/company_website_finder.py's own corroboration guards).
    Business logic lives in batch/supplier_correction.py's SupplierCorrectionService,
    shared with POST /suppliers/{id}/correct-domain (this codebase's production database
    is a SQLite file on a Railway volume, not network-reachable, so an already-deployed
    bad record can only be corrected over HTTP -- this CLI command only ever reaches a
    database on the same filesystem it's run from). Every correction goes through the
    same real pipeline every other write in this codebase does (CompanyWebsiteFinder +
    CollectionService) with a supplier_change_log entry, never a hand-applied database
    patch -- see `python main.py history --supplier-id` to review what changed
    afterward. Specify exactly one of --clear-domain, --set-domain, or --flag-duplicate;
    add another --clear-<field>/--set-<field> following the same shape here for a future
    bad-record class, rather than reaching for raw SQL again."""
    from batch.supplier_correction import SupplierCorrectionService

    modes = [bool(clear_domain), bool(set_domain), bool(flag_duplicate)]
    if sum(modes) != 1:
        console.print("[red]X[/red] Specify exactly one of --clear-domain, --set-domain, or --flag-duplicate.")
        raise SystemExit(1)

    service = SupplierCorrectionService()
    try:
        if flag_duplicate:
            result = service.flag_duplicate(supplier_id, flag_duplicate)
        elif set_domain:
            console.print("[bold]Setting confirmed domain directly...[/bold] (no search)")
            result = service.set_confirmed_domain(supplier_id, set_domain, canonical_name=set_name, reason=reason)
        else:
            console.print("[bold]Correcting...[/bold] (1 SerpAPI call, +1 OpenAI call if a candidate "
                           "site needs grounded-name verification)")
            result = service.correct_domain(supplier_id, reason=reason)
    except ValueError as e:
        console.print(f"[red]X[/red] {e}")
        raise SystemExit(1)

    console.print(f"[bold]Supplier #{supplier_id}[/bold]: {result['canonical_name']!r}")
    if result["status"] == "flagged":
        console.print(f"[green]OK[/green] Flagged excluded: {result['flag_reason']}")
        return
    console.print(f"(was domain: {result['old_domain']!r})")
    if result["status"] == "needs_url":
        console.print(f"[yellow]![/yellow] No validated replacement found ({result['reason']}). "
                       f"Left with no domain -- re-run `python main.py find-websites` later, "
                       f"or set the correct domain manually once you have it.")
        return
    if result["status"] == "domain_conflict":
        console.print(f"[yellow]![/yellow] {result['reason']}")
        return

    console.print(f"[green]OK[/green] Domain set: {result['new_domain']}")
    console.print(f"[green]OK[/green] Re-collected: {result['collection_status']} "
                   f"({result['pages_visited']} page(s) visited).")


@cli.command("enable-monitoring")
@click.option("--supplier-id", type=int, required=True)
@click.option("--cadence", type=click.Choice(["monthly", "quarterly"]), required=True)
def enable_monitoring_cmd(supplier_id: int, cadence: str) -> None:
    """Opt one supplier into recurring monitoring (certifications, contact fields,
    Companies House status, website reachability -- see monitoring/monitoring_service.py
    for exactly what's tracked and why every v1 field is free). Shows the real cost
    disclosure before enabling -- this is the one and only confirmation point for these
    free fields; capture_snapshot_pending itself runs unattended on a schedule with no
    per-run prompt, since there's no human present when a monthly check actually fires."""
    from monitoring.monitoring_service import MONITORING_COST_DISCLOSURE, MonitoringService

    console.print(f"[bold]Cost:[/bold] {MONITORING_COST_DISCLOSURE}")
    service = MonitoringService()
    try:
        result = service.enable_monitoring(supplier_id, cadence)
    except ValueError as e:
        console.print(f"[red]X[/red] {e}")
        raise SystemExit(1)
    console.print(f"[green]OK[/green] Supplier #{supplier_id} monitoring enabled "
                   f"({cadence}, next check due {result['next_check_due_at']}).")


@cli.command("run-monitoring-checks")
@click.option("--limit", default=None, type=int,
              help="Cap how many due suppliers to check this run. Omit to process every "
                   "supplier currently due (next_check_due_at <= now).")
def run_monitoring_checks_cmd(limit: Optional[int]) -> None:
    """Process every supplier currently due for a recurring monitoring re-check
    (see main.py's enable-monitoring). Intended to be invoked periodically by an
    external scheduler (a real OS cron / Railway scheduled job) -- this codebase has no
    in-app cron of its own, same limitation every other async stage already documents.
    Every v1 tracked field is free, so this prints scope after running rather than
    blocking on a confirmation prompt before it -- see monitoring/monitoring_service.py's
    own module docstring for the standing guard against a future paid field silently
    inheriting this no-confirmation default."""
    from monitoring.monitoring_service import MonitoringService

    service = MonitoringService()
    result = service.capture_snapshot_pending(limit=limit)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="magenta")
    for key, value in result.items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)


@cli.command("check-linkedin")
@click.option("--force", is_flag=True, help="Re-check every supplier, not just ones never checked.")
def check_linkedin(force: bool) -> None:
    """Confirm whether each supplier has a findable LinkedIn company page, via a
    Google-search snippet — never scrapes LinkedIn directly (see the module
    docstring in verification/linkedin_presence.py). A hit is one corroborating
    fact, not a manufacturer-vs-trader verdict on its own."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_linkedin_check_only(force=force)
    console.print(f"[green]OK[/green] Found a LinkedIn page for {stats['linkedin_checked']} supplier(s).")


@cli.command("extract-capabilities")
@click.option("--force", is_flag=True,
              help="Re-attempt every supplier with a known domain, not just ones never attempted.")
@click.option("--limit", default=1000, show_default=True,
              help="Stop after this many suppliers. Use a small number on a first run — this "
                   "spends real money per supplier and is worth checking before running at scale.")
@click.option("--no-photos", is_flag=True,
              help="Skip factory-photo assessment. Photos go to a vision model (up to 5 images per "
                   "page, several pages per supplier) and are by far the most expensive part of this "
                   "command, so a first calibration run is usually cheaper without them.")
def extract_capabilities(force: bool, limit: int, no_photos: bool) -> None:
    """Crawl each supplier's own website for manufacturing capability evidence
    (processes, capabilities, standards) — the one signal none of the directory/
    registry-based sources can give you: what a company says about itself, in
    its own words, distinguishing what it makes from what it merely sells.

    On a first run, start small:

        python main.py extract-capabilities --limit 30 --no-photos
    """
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_capability_extraction_only(
        force=force, limit=limit, assess_photos=not no_photos
    )

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    for key in ("capability_extracted", "contact_emails_added", "contact_phones_added",
                "contact_forms_recorded", "photos_assessed"):
        table.add_row(key.replace("_", " "), str(stats.get(key, 0)))
    console.print(table)
    if no_photos:
        console.print("[dim]Photo assessment skipped. Drop --no-photos to include it.[/dim]")


@cli.command("extract-catalogue-depth")
@click.option("--supplier-id", type=int, default=None, help="Extract for one specific supplier by id.")
@click.option("--pending", is_flag=True,
              help="Batch mode: extract for every supplier needing it (never attempted before).")
@click.option("--limit", default=20, show_default=True,
              help="Stop after this many suppliers in --pending mode. Each one costs a real gpt-4o "
                   "call per page (no new HTTP fetch -- reads already-collected pages on disk; gpt-4o, "
                   "not the cheaper gpt-4o-mini most other extraction stages use -- confirmed live that "
                   "mini misses even clear-cut evidence on this specific task), start small on a first run.")
@click.option("--force", is_flag=True,
              help="In --pending mode, re-extract every supplier, not just ones never attempted.")
def extract_catalogue_depth(supplier_id: Optional[int], pending: bool, limit: int, force: bool) -> None:
    """Evidence-only catalogue-depth signals (customer-logos section, named
    case studies, specific process/machine detail vs. generic marketing
    language) from a supplier's own ALREADY-COLLECTED website pages -- no new
    crawl, just an OpenAI call per supplier reading HTML already saved on
    disk by a prior `batch-upload`/`collect` run. Never writes a verdict --
    purely evidence for criterion A (Website Deep-Dive) in the buyer tracker
    export, same discipline as Certifications Claimed. A supplier with no
    successful collection run on file is skipped (marked attempted, not
    retried forever), not fetched live -- see
    verification/catalogue_depth_service.py's own docstring, including a
    real caveat about source-URL precision worth reading before trusting
    the evidence column's links.

    Either --supplier-id ONE or --pending a batch. On a first run, start
    small:

        python main.py extract-catalogue-depth --pending --limit 5
    """
    from verification.catalogue_depth_service import CatalogueDepthService

    if not supplier_id and not pending:
        console.print("[red]X[/red] Specify either --supplier-id or --pending.")
        return

    service = CatalogueDepthService()
    if supplier_id:
        outcome = service.extract(supplier_id)
        console.print(f"[green]OK[/green] Supplier #{supplier_id}: {outcome}")
        return

    outcome = service.extract_pending(limit=limit, force=force)
    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    for key in ("attempted", "extracted", "unavailable", "total_eligible", "status"):
        table.add_row(key.replace("_", " "), str(outcome.get(key, "")))
    console.print(table)


@cli.command("import-ris-findings")
@click.argument("xlsx_path", type=click.Path(exists=True))
def import_ris_findings_cmd(xlsx_path: str) -> None:
    """One-time import of reverse-image-search evidence (Verification Flag,
    Exact Duplicate Domains, Other Matching Domains) from an external local
    geocode/Street-View/RIS pipeline's own spreadsheet -- there is no live RIS
    pipeline in this codebase; this reads a "Supplier Audit"-sheet-shaped xlsx
    (same columns build_tracker_workbook produces, plus those three) and
    writes them onto each matched supplier's row. Matches by domain first,
    falls back to fuzzy company-name matching -- see batch/ris_importer.py's
    own docstring. Every unmatched row is reported, never silently dropped.
    Safe to re-run (overwrites with the same values on a re-import of the
    same file)."""
    from batch.ris_importer import import_ris_findings

    result = import_ris_findings(xlsx_path)

    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="magenta")
    table.add_row("matched by domain", str(result.matched_by_domain))
    table.add_row("matched by name (fuzzy fallback)", str(result.matched_by_name))
    table.add_row("imported", str(len(result.imported_supplier_ids)))
    table.add_row("unmatched", str(len(result.unmatched)))
    console.print(table)

    if result.unmatched:
        console.print("\n[yellow]! Unmatched rows (no supplier found by domain or fuzzy name):[/yellow]")
        for row in result.unmatched:
            console.print(f"    - {row.get('Supplier Name')!r} ({row.get('Website')!r})")


@cli.command("set-verdict")
@click.option("--supplier-id", type=int, required=True, help="Supplier to set a verdict on.")
@click.option("--criterion", type=click.Choice(["A", "B", "C", "D", "Qualified", "Notes"]), required=True)
@click.option("--value", default=None,
              help="Pending/Pass/Fail for A-D, Pending/Yes/No for Qualified. Not used for Notes.")
@click.option("--notes", default=None, help="Free text. Only used with --criterion Notes.")
def set_verdict(supplier_id: int, criterion: str, value: Optional[str], notes: Optional[str]) -> None:
    """Set one Audit-tab verdict from the command line -- writes to the
    same audit_verdicts table (and the same upsert_audit_verdict path)
    as clicking a dropdown in the frontend's Audit tab, so tracker export
    and the UI both pick it up immediately. This never computes or infers
    a verdict itself (see CLAUDE.md standing rule 2) -- it only records
    the value you pass in, exactly like a human's own dropdown selection.

        python main.py set-verdict --supplier-id 789 --criterion A --value Pass
        python main.py set-verdict --supplier-id 789 --criterion Qualified --value Yes
        python main.py set-verdict --supplier-id 789 --criterion Notes --notes "Confirmed by phone 2026-08-21"
    """
    if criterion == "Notes" and value is not None:
        console.print("[red]X[/red] --value isn't used with --criterion Notes -- use --notes instead.")
        raise SystemExit(1)
    if criterion != "Notes" and notes is not None:
        console.print(f"[red]X[/red] --notes isn't used with --criterion {criterion} -- Notes is a single "
                       "shared field, set separately with --criterion Notes.")
        raise SystemExit(1)

    repo = SupplierRepository()
    if repo.get_supplier(supplier_id) is None:
        console.print(f"[red]X[/red] Supplier #{supplier_id} not found.")
        raise SystemExit(1)

    try:
        repo.upsert_audit_verdict(supplier_id, criterion, value=value, notes=notes)
    except ValueError as e:
        console.print(f"[red]X[/red] {e}")
        raise SystemExit(1)

    result = repo.get_audit_verdicts(supplier_id).get(criterion, {})
    review_date = repo.get_audit_review_date(supplier_id)
    console.print(
        f"[green]OK[/green] Supplier #{supplier_id} {criterion} -> "
        f"value={result.get('value')!r} notes={result.get('notes')!r} set_at={result.get('set_at')}"
    )
    if review_date:
        console.print(f"[dim]Date Reviewed: {review_date}[/dim]")


@cli.command("search")
@click.argument("product", required=False)
@click.option("--require", "required_capabilities", multiple=True,
              help='Capability/certification that MUST be evidenced, e.g. --require "e-mark approval". '
                   'Repeatable — every one given must be present (AND, not OR).')
@click.option("--manufacturers-only", is_flag=True,
              help="Only suppliers ManufacturerVerifier has actually confirmed make things, "
                   "not just trade them.")
@click.option("--country", default=None,
              help='Exact match against however country is stored, e.g. --country "United Kingdom". '
                   "Deliberately not fuzzy — see search_suppliers_full's own docstring for why.")
@click.option("--min-score", type=int, default=None)
@click.option("--min-confidence", type=float, default=0.0,
              help="Minimum confidence for a --require match to count (0.0-1.0).")
@click.option("--limit", default=25)
def search(product: str, required_capabilities: tuple, manufacturers_only: bool, country: str,
           min_score: int, min_confidence: float, limit: int) -> None:
    """The full procurement search: a product, plus everything it needs to be
    trusted. Combines product matching, capability/certification requirements,
    and manufacturer verification in one call, e.g.:

        python main.py search "wheel bearings" --require "iso 9001" --manufacturers-only

        python main.py search "bespoke roto moulding" \\
            --require "rotational moulding" --require "sub-assembly"

    Run at least one of PRODUCT or --require, or this returns your whole
    catalogue sorted by score."""
    from config.settings import SAFETY_CRITICAL_CATEGORIES

    if not product and not required_capabilities:
        console.print("[yellow]Give a product, at least one --require, or both.[/yellow]")
        return

    repo = SupplierRepository()
    try:
        suppliers = repo.search_suppliers_full(
            product_query=product,
            required_capabilities=list(required_capabilities),
            manufacturers_only=manufacturers_only,
            min_capability_confidence=min_confidence,
            min_score=min_score,
            country=country,
            limit=limit,
        )
    except ValueError as e:
        console.print(f"[red]X[/red] {e}")
        return

    if not suppliers:
        console.print("[yellow]No suppliers matched.[/yellow]")
        return

    from verification.email_deliverability import check_email_domain
    from verification.trust_signals import check_emark_format, check_phone_validity

    for s in suppliers:
        categories = s.get("primary_categories") or []
        flagged = "  [red]! safety-critical part — verify certification evidence[/red]" if (
            set(categories) & SAFETY_CRITICAL_CATEGORIES
        ) else ""

        console.print(f"\n[bold]{s['canonical_name']}[/bold] (#{s['id']}){flagged}")

        year_established = s.get("year_established")
        alibaba_years = s.get("alibaba_years")
        if year_established:
            tenure_bit = f" · est. {year_established}"
        elif alibaba_years:
            tenure_bit = f" · {alibaba_years} yrs on Alibaba"
        else:
            tenure_bit = ""
        console.print(
            f"  {s.get('country') or '-'} · score {s['composite_score']} · {s['recommendation']}"
            + (" · [green]verified manufacturer[/green]" if s.get("is_manufacturer") else "")
            + (" · [green]LinkedIn confirmed[/green]" if s.get("linkedin_url") else "")
            + tenure_bit
        )

        if s.get("facility_address_verified_at"):
            if s.get("facility_address_verified"):
                source = s.get("facility_address_verification_source") or "unknown source"
                console.print(f"  [green]OK[/green] Address verified as a real place ({source})")
            else:
                console.print("  [red]![/red] Address could not be verified — worth a closer look")

        contact_bits = []
        if s.get("primary_email"):
            email_check = check_email_domain(s["primary_email"])
            email_tag = "" if email_check.has_mx_records else " [red](domain cannot receive mail)[/red]"
            contact_bits.append(f"{s['primary_email']}{email_tag}")
        if s.get("primary_phone"):
            phone_check = check_phone_validity(s["primary_phone"])
            phone_tag = "" if phone_check.plausible else " [red](invalid number format)[/red]"
            contact_bits.append(f"{s['primary_phone']}{phone_tag}")
        if contact_bits:
            console.print(f"  Contact: {' / '.join(contact_bits)}")
        elif s.get("contact_form_url"):
            console.print(f"  Contact: [dim]no email/phone found —[/dim] contact form at {s['contact_form_url']}")
        else:
            console.print("  Contact: [dim]none on file[/dim]")

        if s.get("domain"):
            console.print(f"  Website: {s['domain']}")

        if s.get("factory_photo_verdict"):
            verdict = s["factory_photo_verdict"]
            icon = {"plausible_factory": "[green]OK[/green]", "implausible": "[red]![/red]"}.get(
                verdict, "[yellow]?[/yellow]"
            )
            console.print(f"  {icon} Factory photos: {verdict.replace('_', ' ')}")

        # Trade-data client corroboration -- third-party customs
        # records, not self-reported marketing copy. See the "existing
        # clients" row in the feasibility triage this command's
        # docstring links back to: this is the one trustworthy version
        # of that signal.
        shipments = sum(s.get(f"confirmed_shipments_{region}") or 0 for region in ("uk", "eu", "us"))
        if shipments:
            buyers = s.get("known_buyers") or []
            buyer_bit = f" (incl. {', '.join(buyers[:2])})" if buyers else ""
            console.print(f"  [blue]{shipments} confirmed export shipment(s) on customs record{buyer_bit}[/blue]")

        for cap in s.get("matched_capabilities", []):
            if cap["relationship"] == "in_house":
                tag = "in-house"
            elif cap["relationship"] == "subcontracted":
                tag = "subcontracted"
            else:
                tag = "asserted"
            console.print(
                f"  [cyan]OK[/cyan] {cap['canonical_term']} ({tag}, confidence {cap['confidence']:.2f}) "
                f"— \"{cap['evidence']}\""
            )

        for emark_number in (s.get("e_mark_numbers") or []):
            check = check_emark_format(emark_number)
            icon = "[cyan]OK[/cyan]" if check.format_plausible else "[red]![/red]"
            console.print(f"  {icon} E-mark {emark_number} — {check.reason}")

    console.print(f"\n[dim]{len(suppliers)} supplier(s) matched.[/dim]")


@cli.command("find-by-capability")
@click.argument("capability")
@click.option("--relationship", type=click.Choice(["in_house", "subcontracted", "asserted"]), default=None,
              help="Restrict to suppliers with this exact relationship to the capability. "
                   "Default: either — a rotomoulder who subcontracts injection moulding is "
                   "still a legitimate answer to an enquiry needing both processes.")
@click.option("--min-confidence", type=float, default=0.0)
@click.option("--limit", default=25)
def find_by_capability(capability: str, relationship: str, min_confidence: float, limit: int) -> None:
    """Find suppliers with evidence of CAPABILITY on their own website, e.g.

        python main.py find-by-capability "rotational moulding"

    CAPABILITY must be one of the canonical terms in
    verification.capability_vocabulary.VOCABULARY (run this command with an
    unrecognised term and it will simply return no results — recognised terms
    are printed below on a miss so you can check the spelling)."""
    from verification.capability_vocabulary import map_to_canonical

    repo = SupplierRepository()
    mapped = map_to_canonical(capability)
    canonical = mapped.canonical if mapped else capability

    suppliers = repo.find_suppliers_by_capability(
        canonical, relationship=relationship, min_confidence=min_confidence, limit=limit,
    )

    if not suppliers:
        console.print(f"[yellow]No suppliers found for \"{canonical}\".[/yellow]")
        if mapped is None:
            console.print(
                "[dim]This isn't a recognised canonical term — check "
                "verification/capability_vocabulary.py's VOCABULARY for the exact wording, "
                "or run \"extract-capabilities\" first if you haven't yet.[/dim]"
            )
        return

    table = Table(title=f'Suppliers with "{canonical}"' + (f" ({relationship})" if relationship else ""))
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Country")
    table.add_column("Score", justify="right")
    table.add_column("Recommendation")
    for s in suppliers:
        table.add_row(
            str(s["id"]), s["canonical_name"], s.get("country") or "-",
            str(s["composite_score"]), s["recommendation"],
        )
    console.print(table)


@cli.command("certs")
@click.option("--days-ahead", default=90, help="Flag ISO 9001 certs expiring within this many days.")
def certs(days_ahead: int) -> None:
    """Check for expiring/expired ISO 9001 certs and malformed E-mark numbers."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    result = pipeline.check_certificates(days_ahead=days_ahead)

    recheck = result["iso_9001_needing_recheck"]
    malformed = result["malformed_e_mark"]

    console.print(f"[bold]ISO 9001 suppliers needing a recheck ({len(recheck)}):[/bold]")
    for s in recheck:
        console.print(f"  #{s['id']} {s['canonical_name']} — {s['iso_9001_status']}")

    console.print(f"\n[bold]Suppliers with malformed E-mark numbers ({len(malformed)}):[/bold]")
    for s in malformed:
        console.print(f"  #{s['id']} {s['canonical_name']} — {s.get('e_mark_numbers')}")


@cli.command("list")
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "unscored", "avoid"]))
@click.option("--min-score", type=int, default=None)
@click.option("--country", default=None)
@click.option("--limit", default=25)
def list_suppliers(recommendation: str, min_score: int, country: str, limit: int) -> None:
    """List suppliers, optionally filtered by recommendation/score/country."""
    repo = SupplierRepository()
    suppliers = repo.list_suppliers(
        recommendation=recommendation, min_composite_score=min_score, country=country, limit=limit,
    )

    if not suppliers:
        console.print("[yellow]No suppliers matched these filters.[/yellow]")
        return

    table = Table(title=f"Suppliers ({len(suppliers)})")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Country")
    table.add_column("Score", justify="right")
    table.add_column("Recommendation")
    for s in suppliers:
        table.add_row(
            str(s["id"]), s["canonical_name"], s.get("country") or "-",
            str(s["composite_score"]), s["recommendation"],
        )
    console.print(table)


@cli.command("report")
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "unscored", "avoid"]))
@click.option("--min-score", type=int, default=None)
@click.option("--limit", default=50)
@click.option("--output", "-o", default=None, help="Write to this file instead of printing to the console.")
def report(recommendation: str, min_score: int, limit: int, output: str) -> None:
    """Generate a Markdown supplier report."""
    from reports.generator import generate_markdown_report, save_markdown_report

    repo = SupplierRepository()
    if output:
        path = save_markdown_report(repo, output, recommendation=recommendation, min_score=min_score, limit=limit)
        console.print(f"[green]OK[/green] Report written to [bold]{path}[/bold]")
    else:
        console.print(generate_markdown_report(repo, recommendation=recommendation, min_score=min_score, limit=limit))


@cli.command("export-csv")
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "unscored", "avoid"]))
@click.option("--min-score", type=int, default=None)
@click.option("--limit", default=1000)
@click.option("--output", "-o", default="data/exports/suppliers.csv")
def export_csv(recommendation: str, min_score: int, limit: int, output: str) -> None:
    """Export suppliers to a CSV file."""
    from reports.generator import export_suppliers_csv

    repo = SupplierRepository()
    path = export_suppliers_csv(repo, output, recommendation=recommendation, min_score=min_score, limit=limit)
    console.print(f"[green]OK[/green] Exported to [bold]{path}[/bold]")


@cli.command("export-excel")
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "unscored", "avoid"]))
@click.option("--min-score", type=int, default=None)
@click.option("--limit", default=1000)
@click.option("--output", "-o", default="data/exports/suppliers.xlsx")
def export_excel(recommendation: str, min_score: int, limit: int, output: str) -> None:
    """Export suppliers to an Excel (.xlsx) file -- same filters as export-csv,
    plus the contact/address-enrichment columns (secondary emails, contact
    form URL, facility address verification, LinkedIn) CSV leaves out."""
    from reports.generator import export_suppliers_excel

    repo = SupplierRepository()
    path = export_suppliers_excel(repo, output, recommendation=recommendation, min_score=min_score, limit=limit)
    console.print(f"[green]OK[/green] Exported to [bold]{path}[/bold]")


@cli.group("review")
def review() -> None:
    """Manage the human dedup review queue (Phase 1 Gap 6)."""


@review.command("list")
@click.option("--limit", default=25)
def review_list(limit: int) -> None:
    """List pending possible-duplicate suppliers awaiting a merge/reject decision."""
    repo = SupplierRepository()
    candidates = repo.get_pending_review_candidates(limit=limit)

    if not candidates:
        console.print("[green]No pending review candidates.[/green]")
        return

    for c in candidates:
        supplier_a = repo.get_supplier(c["supplier_id_a"])
        supplier_b = repo.get_supplier(c["supplier_id_b"])
        table = Table(title=f"Candidate #{c['id']}  (match score: {c['match_score']:.2f})")
        table.add_column("Field")
        table.add_column(f"A — #{c['supplier_id_a']}")
        table.add_column(f"B — #{c['supplier_id_b']}")
        for field in ("canonical_name", "country", "city", "domain", "uscc"):
            table.add_row(field, str(supplier_a.get(field) or "-"), str(supplier_b.get(field) or "-"))
        console.print(table)
        console.print(f"  Signals: {c.get('match_signals')}\n")


@review.command("merge")
@click.argument("candidate_id", type=int)
@click.option("--keep", type=click.Choice(["a", "b"]), default="b",
              help="Which record survives the merge. Default 'b' (the pre-existing record).")
def review_merge(candidate_id: int, keep: str) -> None:
    """Confirm two candidates are duplicates and merge them into one record."""
    repo = SupplierRepository()
    try:
        kept_id = repo.resolve_review_candidate_as_merge(candidate_id, keep=keep)
    except Exception as e:
        console.print(f"[red]X Could not merge: {e}[/red]")
        return
    console.print(f"[green]OK[/green] Merged. Surviving supplier: #{kept_id}")


@review.command("reject")
@click.argument("candidate_id", type=int)
def review_reject(candidate_id: int) -> None:
    """Confirm two candidates are genuinely different suppliers — leave both records untouched."""
    repo = SupplierRepository()
    try:
        repo.resolve_review_candidate_as_reject(candidate_id)
    except ValueError as e:
        console.print(f"[red]X {e}[/red]")
        return
    console.print(f"[green]OK[/green] Candidate #{candidate_id} marked rejected — both records kept as-is.")


@cli.command("sweep")
@click.option("--source", "-s", "sources", multiple=True, help="Limit to specific source(s). Default: all.")
@click.option("--incremental-days", default=30, help="Skip (source, query) pairs scraped within this many days.")
@click.option("--term-limit", default=None, type=int,
              help="Only run the first N search terms (useful for a quick test sweep).")
@click.option("--limit", "results_limit", type=int, default=None,
              help="Cap raw results kept PER SOURCE, PER TERM to this many -- same cost-control "
                   "flag as `run --limit`, applied to every term in the sweep. With ~40 terms in "
                   "the catalogue this multiplies fast, so start small on a first real sweep. No "
                   "cap by default, matching this command's prior behaviour.")
@click.option("--no-verify", is_flag=True)
@click.option("--no-score", is_flag=True)
def sweep(sources: tuple, incremental_days: int, term_limit: Optional[int], results_limit: Optional[int],
          no_verify: bool, no_score: bool) -> None:
    """Sweep the pipeline across the full trailer-component search-term catalogue."""
    from config.settings import TRAILER_COMPONENT_SEARCH_TERMS
    from pipeline.orchestrator import SupplierIntelligencePipeline, build_limit_scraper_kwargs

    queries = list(TRAILER_COMPONENT_SEARCH_TERMS)
    if term_limit:
        queries = queries[:term_limit]

    console.print(f"[bold]Sweeping {len(queries)} search terms across "
                  f"{', '.join(sources) if sources else 'all configured sources'}...[/bold]\n")

    pipeline = SupplierIntelligencePipeline()
    scraper_kwargs = build_limit_scraper_kwargs(results_limit, list(sources) or None, list(pipeline.scrapers.keys()))
    result = pipeline.run_campaign(
        queries=queries,
        sources=list(sources) or None,
        incremental_days=incremental_days,
        run_verification=not no_verify,
        run_scoring=not no_score,
        scraper_kwargs=scraper_kwargs,
        results_limit=results_limit,
    )

    console.print(f"\n[bold]Campaign complete:[/bold] {result['queries_run']}/{result['queries_total']} "
                  f"queries run ({result['queries_failed']} failed)\n")
    table = Table(show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Total", justify="right", style="magenta")
    for key, value in result["totals"].items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)


@cli.command("search-web")
@click.argument("query")
@click.option("--site-filter", default=None, help="Restrict to one domain, e.g. --site-filter alibaba.com")
@click.option("--limit", default=20)
def search_web(query: str, site_filter: str, limit: int) -> None:
    """Search the web via Google (SerpAPI) for manufacturer websites outside the usual B2B platforms."""
    from scrapers.google_search_scraper import GoogleSearchScraper

    scraper = GoogleSearchScraper()
    results = scraper.scrape(query, max_results=limit, site_filter=site_filter)

    if len(results) == 1 and not results[0].success:
        console.print(f"[red]X {results[0].error}[/red]")
        return

    table = Table(title=f"Web search: \"{query}\"" + (f" (site:{site_filter})" if site_filter else ""))
    table.add_column("Title")
    table.add_column("Link")
    for r in results:
        table.add_row(r.raw_data.get("title", ""), r.raw_data.get("link", ""))
    console.print(table)


@cli.command("match-product")
@click.option("--reference", "-r", "references", multiple=True, required=True,
              type=click.Path(exists=True), help="One or more reference photos of the product you want. Repeatable.")
@click.option("--candidate", "-c", required=True, type=click.Path(exists=True),
              help="The candidate listing photo to check against the reference(s).")
@click.option("--context", default="", help="Optional text description of what you're looking for.")
def match_product(references: tuple, candidate: str, context: str) -> None:
    """Check whether a candidate product photo matches one or more reference photos —
    e.g. confirm a listing is the complete assembly you want, not just a sub-component."""
    import mimetypes
    from verification.product_matcher import ProductMatcher

    def load_photo(path: str) -> dict:
        media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as f:
            return {"image_bytes": f.read(), "media_type": media_type}

    matcher = ProductMatcher()
    reference_photos = [load_photo(p) for p in references]
    candidate_photo = load_photo(candidate)

    result = matcher.compare_to_references(reference_photos, candidate_photo, product_context=context)

    verdict_color = {"match": "green", "partial_match": "yellow", "no_match": "red", "uncertain": "yellow"}.get(result["verdict"], "white")
    console.print(f"[bold]Verdict:[/bold] [{verdict_color}]{result['verdict']}[/{verdict_color}]  "
                  f"(compared against {result['reference_count']} reference photo(s))")
    console.print(f"[bold]Reasoning:[/bold] {result['reasoning']}")


# Countries that can realistically quote DDP in EUR/GBP on open account —
# the shape of IWT's commercial terms, not a judgement on capability.
DDP_NATIVE_COUNTRIES = [
    "Germany", "Italy", "Poland", "Türkiye", "Turkey", "Spain", "France",
    "Netherlands", "Belgium", "Czech Republic", "Slovakia", "Slovenia",
    "Austria", "Portugal", "Sweden", "Denmark", "Hungary", "Romania",
    "Lithuania", "Ireland", "Great Britain and Northern Ireland",
]

_STATUS_STYLE = {
    "EMPTY": "bold red",
    "CRITICAL": "red",
    "THIN": "yellow",
    "OK": "green",
}


@cli.command("coverage")
@click.option("--gaps-only", is_flag=True, help="Hide categories already at target.")
@click.option("--group", default=None, help="Filter to one BOM group, e.g. 'Hardware'.")
@click.option("--tier", type=click.Choice(["A", "B", "C"]), default=None,
              help="Filter to one criticality tier.")
@click.option("--ddp-native", is_flag=True,
              help="Count only suppliers in countries that quote DDP in EUR/GBP.")
@click.option("--detail", is_flag=True, help="Show top sourcing countries per category.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def coverage(gaps_only: bool, group: str, tier: str, ddp_native: bool,
             detail: bool, as_json: bool) -> None:
    """
    BOM category coverage vs shortlist targets.

    Answers "what can't we source yet?" rather than "how many rows do we
    have?". Use the gap column to decide what to scrape next — expansion
    driven by coverage holes beats expansion driven by scraper convenience.
    """
    import json as _json
    from reports.coverage import analyse_coverage, BOM_CATEGORIES

    result = analyse_coverage(
        countries=DDP_NATIVE_COUNTRIES if ddp_native else None,
    )
    rows = result["categories"]

    if group:
        rows = [r for r in rows if r["group"].lower() == group.lower()]
    if tier:
        rows = [r for r in rows if r["tier"] == tier]
    if gaps_only:
        rows = [r for r in rows if r["gap"] > 0]

    if as_json:
        console.print_json(_json.dumps({**result, "categories": rows}))
        return

    if not rows:
        console.print("[green]Every category in this filter is at target.[/green]")
        return

    rows.sort(key=lambda r: (-r["gap"], r["label"]))

    scope = " (DDP-native countries only)" if ddp_native else ""
    table = Table(title=f"BOM Coverage vs Target{scope}")
    table.add_column("Category")
    table.add_column("Group", style="dim")
    table.add_column("Tier", justify="center")
    table.add_column("Have", justify="right")
    table.add_column("Site", justify="right")
    table.add_column("Email", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Gap", justify="right")
    table.add_column("Status")
    if detail:
        table.add_column("Top countries", style="dim")

    for r in rows:
        cells = [
            r["label"],
            r["group"],
            r["tier"],
            str(r["total"]),
            str(r["with_domain"]),
            str(r["with_email"]),
            str(r["target"]),
            f"[bold]{r['gap']}[/bold]" if r["gap"] else "-",
            f"[{_STATUS_STYLE[r['status']]}]{r['status']}[/{_STATUS_STYLE[r['status']]}]",
        ]
        if detail:
            cells.append(", ".join(f"{c} {n}" for c, n in r["top_countries"]) or "-")
        table.add_row(*cells)

    console.print(table)
    console.print(
        f"\n[bold]{result['covered_categories']}/{len(result['categories'])}[/bold] "
        f"categories at target · "
        f"[bold]{result['total_gap']}[/bold] suppliers short of a full shortlist · "
        f"{result['suppliers_analysed']} analysed · "
        f"{result['unmatched']} matched no category"
    )
    if result["unmatched"]:
        console.print(
            f"[dim]{result['unmatched']} suppliers matched no BOM category — either "
            f"out of scope, or the taxonomy in reports/coverage.py needs a term "
            f"adding. Worth eyeballing before trusting the gaps.[/dim]"
        )


if __name__ == "__main__":
    cli()
