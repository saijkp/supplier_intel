"""
main.py

CLI entry point for the Supplier Intelligence Platform.

Usage:
    python main.py init-db                     Create the database and all tables
    python main.py status                      Show schema version and row counts
    python main.py doctor                      Check config / API keys / DB health
    python main.py run "LED marker light"      Run the full scrape->dedup->verify->score pipeline
    python main.py verify                      Re-run Qichacha verification only
    python main.py rescore                     Re-run scoring only
    python main.py certs                       Check for expiring/malformed certifications
    python main.py search "wheel bearings" --require "e-mark approval" --manufacturers-only
                                            The full procurement search: product + required certifications + verified-manufacturer filter, one call
    python main.py find-websites               Find a domain for suppliers whose listing didn't give one
    python main.py extract-capabilities        Crawl suppliers' own websites for manufacturing capability evidence
    python main.py find-by-capability "X"      Find suppliers with capability evidence for X, e.g. "rotational moulding"
    python main.py list                        List suppliers (filterable)
    python main.py report                      Generate a Markdown supplier report
    python main.py export-csv                  Export suppliers to CSV
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
    console.print(f"[green]✓[/green] Database initialised at [bold]{DB_PATH}[/bold]")
    console.print(f"[green]✓[/green] Schema version: [bold]{version}[/bold]")


@cli.command("status")
def status() -> None:
    """Show schema version and current row counts for every table."""
    if not DB_PATH.exists():
        console.print(
            f"[yellow]No database found at {DB_PATH}. "
            f"Run `python main.py init-db` first.[/yellow]"
        )
        sys.exit(1)

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
        console.print(f"[yellow]⚠ Missing API keys (needed for live scraping): {', '.join(missing)}[/yellow]")
        console.print("  Set these in a .env file at the project root.\n")
    else:
        console.print("[green]✓[/green] All required API keys are set.\n")

    weight_sum = round(sum(SCORING_WEIGHTS.values()), 6)
    console.print(f"[green]✓[/green] Scoring weights sum to {weight_sum} ({SCORING_WEIGHTS})")
    console.print(f"[green]✓[/green] Tracking {len(TRAILER_HS_CODES)} HS codes for trailer components")

    if DB_PATH.exists():
        console.print(f"[green]✓[/green] Database exists at {DB_PATH} (schema v{get_schema_version()})")
    else:
        console.print(f"[yellow]⚠ Database not yet created. Run `python main.py init-db`.[/yellow]")

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
    console.print(f"[green]✓[/green] Verified {stats['verified']} supplier(s).")


@cli.command("rescore")
def rescore() -> None:
    """Re-run scoring for any currently-unscored suppliers."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_scoring_only()
    console.print(f"[green]✓[/green] Scored {stats['scored']} supplier(s).")


@cli.command("assess-manufacturers")
@click.option("--force", is_flag=True, help="Re-assess every supplier, not just never-assessed ones.")
def assess_manufacturers(force: bool) -> None:
    """Re-run manufacturer verification (business scope, registered capital,
    tenure consistency, certifications) for suppliers needing assessment."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_manufacturer_assessment_only(force=force)
    console.print(f"[green]✓[/green] Assessed {stats['manufacturer_assessed']} supplier(s).")


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
    console.print(f"[green]✓[/green] Found and validated {stats['website_discovered']} new website(s).")


@cli.command("verify-facilities")
@click.option("--force", is_flag=True, help="Re-check every supplier with an address, not just new ones.")
def verify_facilities(force: bool) -> None:
    """Confirm each supplier's claimed address resolves to a real place. Routes
    China to Amap, everywhere else to Google Places — see the module docstring in
    verification/facility_address_verifier.py for why, and for the real
    registration friction to expect on the China path specifically. Photo
    verification runs separately, as part of "extract-capabilities" — it reuses
    that command's own website fetch rather than needing its own."""
    from pipeline.orchestrator import SupplierIntelligencePipeline

    pipeline = SupplierIntelligencePipeline()
    stats = pipeline.run_facility_verification_only(force=force)
    console.print(f"[green]✓[/green] Verified {stats['facility_address_verified']} address(es) as real places.")


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
    console.print(f"[green]✓[/green] Found a LinkedIn page for {stats['linkedin_checked']} supplier(s).")


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
        console.print(f"[red]✗[/red] {e}")
        return

    if not suppliers:
        console.print("[yellow]No suppliers matched.[/yellow]")
        return

    from verification.email_deliverability import check_email_domain
    from verification.trust_signals import check_emark_format, check_phone_validity

    for s in suppliers:
        categories = s.get("primary_categories") or []
        flagged = "  [red]⚠ safety-critical part — verify certification evidence[/red]" if (
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
                console.print(f"  [green]✓[/green] Address verified as a real place ({source})")
            else:
                console.print("  [red]⚠[/red] Address could not be verified — worth a closer look")

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
            icon = {"plausible_factory": "[green]✓[/green]", "implausible": "[red]⚠[/red]"}.get(
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
                f"  [cyan]✓[/cyan] {cap['canonical_term']} ({tag}, confidence {cap['confidence']:.2f}) "
                f"— \"{cap['evidence']}\""
            )

        for emark_number in (s.get("e_mark_numbers") or []):
            check = check_emark_format(emark_number)
            icon = "[cyan]✓[/cyan]" if check.format_plausible else "[red]⚠[/red]"
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
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "avoid"]))
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
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "avoid"]))
@click.option("--min-score", type=int, default=None)
@click.option("--limit", default=50)
@click.option("--output", "-o", default=None, help="Write to this file instead of printing to the console.")
def report(recommendation: str, min_score: int, limit: int, output: str) -> None:
    """Generate a Markdown supplier report."""
    from reports.generator import generate_markdown_report, save_markdown_report

    repo = SupplierRepository()
    if output:
        path = save_markdown_report(repo, output, recommendation=recommendation, min_score=min_score, limit=limit)
        console.print(f"[green]✓[/green] Report written to [bold]{path}[/bold]")
    else:
        console.print(generate_markdown_report(repo, recommendation=recommendation, min_score=min_score, limit=limit))


@cli.command("export-csv")
@click.option("--recommendation", type=click.Choice(["recommended", "review", "unverified", "avoid"]))
@click.option("--min-score", type=int, default=None)
@click.option("--limit", default=1000)
@click.option("--output", "-o", default="data/exports/suppliers.csv")
def export_csv(recommendation: str, min_score: int, limit: int, output: str) -> None:
    """Export suppliers to a CSV file."""
    from reports.generator import export_suppliers_csv

    repo = SupplierRepository()
    path = export_suppliers_csv(repo, output, recommendation=recommendation, min_score=min_score, limit=limit)
    console.print(f"[green]✓[/green] Exported to [bold]{path}[/bold]")


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
        console.print(f"[red]✗ Could not merge: {e}[/red]")
        return
    console.print(f"[green]✓[/green] Merged. Surviving supplier: #{kept_id}")


@review.command("reject")
@click.argument("candidate_id", type=int)
def review_reject(candidate_id: int) -> None:
    """Confirm two candidates are genuinely different suppliers — leave both records untouched."""
    repo = SupplierRepository()
    try:
        repo.resolve_review_candidate_as_reject(candidate_id)
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        return
    console.print(f"[green]✓[/green] Candidate #{candidate_id} marked rejected — both records kept as-is.")


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
        console.print(f"[red]✗ {results[0].error}[/red]")
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


if __name__ == "__main__":
    cli()
