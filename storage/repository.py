"""
storage/repository.py

Complete data access layer for the Supplier Intelligence Platform.
Every read/write to SQLite goes through this module — scrapers,
normalizers, the dedup matcher, the scorer, and the pipeline
orchestrator never touch sqlite3 directly.

Conventions:
- All public methods return plain dicts (or lists of dicts), never
  sqlite3.Row objects, so callers don't need to know about the storage
  layer's internals.
- Columns that store JSON arrays/objects are transparently serialised
  on write and parsed back to Python lists/dicts on read.
- Every method opens and closes its own connection via
  `database.connection_scope`. This keeps the repository simple and
  safe to use from a single-threaded CLI/pipeline; if concurrent
  writers are introduced later, this is the place to add locking or
  move to a connection pool.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from storage.database import connection_scope

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Column whitelists
# ─────────────────────────────────────────────────────────────

# Columns on `suppliers` that store JSON-encoded arrays. Anything
# written to these columns via create/merge is json.dumps()'d; anything
# read back is json.loads()'d.
SUPPLIER_JSON_FIELDS: Sequence[str] = (
    "aliases",
    "secondary_emails",
    "contact_source_pages",
    "e_mark_numbers",
    "other_certifications",
    "primary_categories",
    "product_keywords",
    "trailer_components",
    "active_export_countries",
    "known_buyers",
    "payment_terms_offered",
    "incoterms_supported",
    "currencies_accepted",
    "manufacturer_signals",
    "factory_photo_urls",
    "candidate_facility_photo_urls",
    "ai_strengths",
    "ai_risks",
    "ai_suitable_customer_types",
    "key_contacts",
    "ai_confidence_breakdown",
    "certificate_document_urls",
    "companies_house_sic_codes",
)

# All columns on `suppliers` that may be set directly via
# create_golden_record / merge_into_golden. Excludes id and the
# timestamp columns that the DB / repository manage automatically.
SUPPLIER_WRITABLE_FIELDS: Sequence[str] = (
    "canonical_name", "aliases", "domain",
    "country", "province_state", "city", "address",
    "factory_location", "candidate_facility_photo_urls",
    "primary_email", "secondary_emails", "primary_phone",
    "whatsapp", "wechat_id", "contact_source_pages",
    "linkedin_url", "contact_name", "contact_title",
    "uscc", "uscc_verified", "uscc_verified_at", "company_reg_number",
    "is_manufacturer", "manufacturer_confidence", "year_established",
    "employee_count", "factory_size_sqm", "annual_revenue_usd",
    "iso_9001", "iso_9001_expiry", "iso_ts_16949",
    "e_mark_certified", "e_mark_numbers", "ce_certified", "ukca_certified",
    "iatf_16949", "other_certifications",
    "primary_categories", "product_keywords", "trailer_components", "moq_notes",
    "exports_to_uk", "exports_to_eu", "exports_to_us", "active_export_countries",
    "confirmed_shipments_uk", "confirmed_shipments_eu", "confirmed_shipments_us",
    "last_shipment_date", "annual_export_volume", "known_buyers", "sinosure_coverage",
    "payment_terms_offered", "incoterms_supported", "currencies_accepted", "can_do_ddp_uk",
    "alibaba_url", "alibaba_gold_supplier", "alibaba_years",
    "alibaba_trade_assurance", "alibaba_rating",
    "indiamart_url", "hktdc_url", "made_in_china_url",
    "notes", "flagged", "flag_reason",
    "manufacturer_signals", "registered_capital_rmb", "business_scope",
    "manufacturer_verified_at", "factory_photo_urls", "factory_photo_verdict",
    "factory_photo_assessed_at", "capability_extracted_at", "website_search_attempted_at",
    "facility_address_verified", "facility_address_verification_source", "facility_address_verified_at",
    "linkedin_checked_at", "contact_form_url",
    "ai_confidence_score", "ai_confidence_assessed_at", "ai_summary", "ai_strengths",
    "ai_risks", "ai_suitable_customer_types", "ai_verification_model",
    "discovery_source", "collection_last_run_at", "collection_status", "last_verified",
    "sourcing_oem_odm_notes", "sourcing_factory_notes", "sourcing_engineering_notes",
    "sourcing_export_notes", "sourcing_volume_suitability", "sourcing_payment_terms_notes",
    "sourcing_verification_status", "key_contacts", "contacts_found_at",
    "ai_confidence_breakdown", "procurement_recommendation", "procurement_recommendation_reason",
    "certificate_document_urls", "production_lines_notes", "machinery_notes",
    "factory_ownership", "factory_facts_extracted_at",
    "companies_house_number", "companies_house_status", "companies_house_registered_office",
    "companies_house_incorporated_at", "companies_house_sic_codes",
    "companies_house_match_status", "companies_house_match_confidence", "companies_house_checked_at",
    "catalogue_depth_extracted_at",
    "ris_verification_flag", "ris_exact_duplicate_domains", "ris_other_matching_domains", "ris_imported_at",
    "reputation_search_attempted_at", "capability_extraction_status",
)

SCORE_FIELDS: Sequence[str] = (
    "product_fit_score", "provenance_score", "verification_score", "self_asserted_score",
    "export_score", "platform_score", "contact_score", "composite_score", "evidence_coverage",
    "recommendation",
)


def _json_default(obj: Any) -> str:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def _row_to_dict(row: Optional[sqlite3.Row], json_fields: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """Convert a sqlite3.Row to a plain dict, parsing any JSON columns
    back into native Python lists/dicts. Malformed JSON is left as the
    raw string rather than raising, since a corrupt cell shouldn't
    crash an entire report."""
    if row is None:
        return None
    d = dict(row)
    for field in json_fields:
        if field in d and d[field]:
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def _rows_to_dicts(rows: List[sqlite3.Row], json_fields: Sequence[str] = ()) -> List[Dict[str, Any]]:
    return [_row_to_dict(r, json_fields) for r in rows]


class SupplierRepository:
    """Data access layer. Instantiate once per script/CLI run; each
    method manages its own short-lived connection."""

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = db_path

    # ═════════════════════════════════════════════════════
    # RAW SOURCE DATA
    # ═════════════════════════════════════════════════════

    def save_raw(
        self,
        source: str,
        raw_data: Dict[str, Any],
        source_id: str = "",
        processing_status: str = "pending",
    ) -> int:
        """Persist a raw scrape payload exactly as received. Returns the
        new raw_source_data.id. This should be called immediately after
        a successful scrape, before any normalisation is attempted, so
        nothing is ever lost to a downstream parsing bug."""
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO raw_source_data (source, source_id, raw_json, processing_status)
                VALUES (?, ?, ?, ?)
                """,
                (source, source_id, json.dumps(raw_data, default=_json_default), processing_status),
            )
            return cur.lastrowid

    def mark_raw_processed(
        self,
        raw_id: int,
        golden_record_id: Optional[int] = None,
        status: str = "processed",
        error_message: Optional[str] = None,
    ) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                UPDATE raw_source_data
                SET processing_status = ?, golden_record_id = ?, error_message = ?
                WHERE id = ?
                """,
                (status, golden_record_id, error_message, raw_id),
            )

    def get_raw(self, raw_id: int) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM raw_source_data WHERE id = ?", (raw_id,)
            ).fetchone()
            result = _row_to_dict(row)
            if result and result.get("raw_json"):
                try:
                    result["raw_json"] = json.loads(result["raw_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result

    def get_raw_by_golden_record(self, golden_record_id: int) -> List[Dict[str, Any]]:
        """Every raw_source_data row that fed into a given golden
        record, most recent first -- e.g. discovery.candidate_validator's
        per-candidate validation reason (raw_json.reason), surfaced as
        export-only evidence by batch.tracker_exporter. A supplier
        rediscovered under more than one product term/query has more
        than one row here (each still points at the same
        golden_record_id via merge/dedup), so callers wanting a single
        reason should take the first (most recent) entry."""
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM raw_source_data WHERE golden_record_id = ? ORDER BY scraped_at DESC",
                (golden_record_id,),
            ).fetchall()
            return _rows_to_dicts(rows, json_fields=("raw_json",))

    def get_pending_raw(self, source: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            if source:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_source_data
                    WHERE processing_status = 'pending' AND source = ?
                    ORDER BY scraped_at ASC LIMIT ?
                    """,
                    (source, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_source_data
                    WHERE processing_status = 'pending'
                    ORDER BY scraped_at ASC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows)

    # ═════════════════════════════════════════════════════
    # SUPPLIERS — lookups used by the dedup matcher
    # ═════════════════════════════════════════════════════

    def find_by_uscc(self, uscc: str) -> Optional[Dict[str, Any]]:
        if not uscc:
            return None
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM suppliers WHERE uscc = ?", (uscc,)
            ).fetchone()
            return _row_to_dict(row, SUPPLIER_JSON_FIELDS)

    def find_by_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        if not domain:
            return None
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM suppliers WHERE domain = ?", (domain,)
            ).fetchone()
            return _row_to_dict(row, SUPPLIER_JSON_FIELDS)

    def find_by_country(self, country: str, limit: int = 2000) -> List[Dict[str, Any]]:
        """Used by the fuzzy name matcher to narrow the candidate pool
        before running string similarity. Returns all suppliers when
        country is empty, since an unknown country shouldn't silently
        exclude every possible match."""
        with connection_scope(self.db_path) as conn:
            if country:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE country = ? LIMIT ?",
                    (country, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM suppliers LIMIT ?", (limit,)
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def get_supplier(self, supplier_id: int) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM suppliers WHERE id = ?", (supplier_id,)
            ).fetchone()
            return _row_to_dict(row, SUPPLIER_JSON_FIELDS)

    def list_suppliers(
        self,
        recommendation: Optional[str] = None,
        country: Optional[str] = None,
        min_composite_score: Optional[int] = None,
        ids: Optional[Sequence[int]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """General-purpose filtered listing, used by reports and the CLI.
        `ids`, if given, scopes to exactly those supplier ids -- used by
        GET /sourcing/runs/{id}/export.csv to download exactly the
        suppliers one sourcing_runs row qualified, not the whole table."""
        clauses: List[str] = []
        params: List[Any] = []

        if recommendation:
            clauses.append("recommendation = ?")
            params.append(recommendation)
        if country:
            clauses.append("country = ?")
            params.append(country)
        if min_composite_score is not None:
            clauses.append("composite_score >= ?")
            params.append(min_composite_score)
        if ids is not None:
            if not ids:
                return []
            placeholders = ", ".join("?" for _ in ids)
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])

        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM suppliers
                {where}
                ORDER BY composite_score DESC, canonical_name ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def search_suppliers(self, keyword: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Simple LIKE-based search across name, keywords, and categories.
        Fine for Phase 1 volumes; swap for FTS5 once the table grows large.

        Excludes flagged=1 suppliers -- a human-only "not a valid
        candidate" marker (see update_supplier_fields_with_history's
        flagged/flag_reason usage), so a record someone has explicitly
        ruled out (e.g. a broker/network, not a single factory) can't
        silently resurface here just because a later search term
        happens to match it again."""
        pattern = f"%{keyword}%"
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM suppliers
                WHERE (canonical_name LIKE ?
                   OR product_keywords LIKE ?
                   OR primary_categories LIKE ?
                   OR trailer_components LIKE ?)
                   AND flagged = 0
                ORDER BY composite_score DESC
                LIMIT ?
                """,
                (pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def search_suppliers_full(
        self,
        *,
        product_query: Optional[str] = None,
        required_capabilities: Optional[List[str]] = None,
        manufacturers_only: bool = False,
        min_capability_confidence: float = 0.0,
        min_score: Optional[int] = None,
        country: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """The single query a procurement search is actually built
        from: a product ("wheel bearings"), a set of capabilities or
        certifications that must ALL be evidenced (E-mark, ISO 9001,
        bespoke/low-volume assembly, ...), and optionally restricted to
        suppliers `ManufacturerVerifier` has actually confirmed make
        things rather than trade them. Every one of those three axes
        already existed as its own separate query
        (`search_suppliers`, `find_suppliers_by_capability`,
        `is_manufacturer`) -- this is the join that was missing, not a
        new capability.

        `required_capabilities` is AND, not OR: a request for
        `["e-mark approval", "sub-assembly"]` returns only suppliers
        with evidence of *both*, via the standard SQL relational-
        division pattern (GROUP BY supplier_id HAVING COUNT(DISTINCT
        canonical_term) = the number required) -- the same
        one-row-per-supplier semantics `find_suppliers_by_capability`
        already established for a single term, extended rather than
        replaced. An unrecognised term raises `ValueError` naming the
        bad term, rather than silently matching nothing -- a typo'd
        certification requirement should never look identical to "no
        supplier has this."

        `country` is an exact, case-insensitive match against however
        the value is actually stored ("China", "United Kingdom") --
        deliberately not fuzzy. A loose match risks "UK" silently
        matching "Ukraine"; getting that wrong in a procurement filter
        is worse than requiring the caller to pass the exact stored
        value. If you need "UK" to resolve to "United Kingdom"
        specifically, normalise it before calling this (pycountry is
        already a project dependency, unused elsewhere, and is the
        natural place to add that if it becomes a real pain point).

        Each result carries a `matched_capabilities` list (canonical
        term, relationship, confidence, evidence quote, source URL) so
        the caller can show *why* a supplier matched, not just that it
        did -- the evidence is exactly why this whole capability
        layer was built, and it should never be thrown away between
        the query and the display.

        Always excludes flagged=1 suppliers (unconditionally, not one
        of the optional filters above) -- same human-only "not a valid
        candidate" marker search_suppliers excludes, so a record
        someone has explicitly ruled out can't silently resurface as a
        procurement search result later.
        """
        from verification.capability_vocabulary import map_to_canonical

        required_capabilities = required_capabilities or []
        canonical_terms = []
        for term in required_capabilities:
            mapped = map_to_canonical(term)
            if mapped is None:
                raise ValueError(
                    f"{term!r} is not a recognised capability/certification term -- "
                    f"see verification/capability_vocabulary.py's VOCABULARY for valid terms"
                )
            canonical_terms.append(mapped.canonical)

        clauses: List[str] = ["flagged = 0"]
        params: List[Any] = []

        if product_query:
            pattern = f"%{product_query}%"
            clauses.append(
                "(canonical_name LIKE ? OR product_keywords LIKE ? "
                "OR primary_categories LIKE ? OR trailer_components LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])

        if manufacturers_only:
            clauses.append("is_manufacturer = 1")

        if min_score is not None:
            clauses.append("composite_score >= ?")
            params.append(min_score)

        if country:
            clauses.append("LOWER(country) = LOWER(?)")
            params.append(country)

        if canonical_terms:
            placeholders = ", ".join("?" for _ in canonical_terms)
            clauses.append(
                f"""id IN (
                    SELECT supplier_id FROM supplier_capabilities
                    WHERE canonical_term IN ({placeholders}) AND confidence >= ?
                    GROUP BY supplier_id
                    HAVING COUNT(DISTINCT canonical_term) = ?
                )"""
            )
            params.extend([*canonical_terms, min_capability_confidence, len(canonical_terms)])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM suppliers
                {where}
                ORDER BY composite_score DESC, canonical_name ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            suppliers = _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

            if canonical_terms and suppliers:
                supplier_ids = [s["id"] for s in suppliers]
                id_placeholders = ", ".join("?" for _ in supplier_ids)
                term_placeholders = ", ".join("?" for _ in canonical_terms)
                capability_rows = conn.execute(
                    f"""
                    SELECT * FROM supplier_capabilities
                    WHERE supplier_id IN ({id_placeholders})
                    AND canonical_term IN ({term_placeholders})
                    """,
                    [*supplier_ids, *canonical_terms],
                ).fetchall()
                capabilities_by_supplier: Dict[int, List[Dict[str, Any]]] = {}
                for row in _rows_to_dicts(capability_rows):
                    capabilities_by_supplier.setdefault(row["supplier_id"], []).append(row)
                for supplier in suppliers:
                    supplier["matched_capabilities"] = capabilities_by_supplier.get(supplier["id"], [])
            else:
                for supplier in suppliers:
                    supplier["matched_capabilities"] = []

            return suppliers

    # ═════════════════════════════════════════════════════
    # SUPPLIERS — write paths (create / merge)
    # ═════════════════════════════════════════════════════

    def _prepare_supplier_payload(self, supplier_data: Dict[str, Any]) -> Dict[str, Any]:
        """Whitelist + JSON-encode a supplier_data dict for insertion.
        Unknown keys are silently dropped rather than raising, since
        normalizers may attach extra scratch fields for their own use."""
        payload: Dict[str, Any] = {}
        for field in SUPPLIER_WRITABLE_FIELDS:
            if field not in supplier_data or supplier_data[field] is None:
                continue
            value = supplier_data[field]
            if field in SUPPLIER_JSON_FIELDS and not isinstance(value, str):
                value = json.dumps(value, default=_json_default)
            payload[field] = value
        return payload

    def create_golden_record(self, supplier_data: Dict[str, Any]) -> int:
        """Insert a brand-new golden supplier record. Requires at least
        canonical_name; every other field is optional."""
        if not supplier_data.get("canonical_name"):
            raise ValueError("supplier_data must include 'canonical_name'")

        payload = self._prepare_supplier_payload(supplier_data)
        columns = list(payload.keys())
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(columns)

        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                f"INSERT INTO suppliers ({column_sql}) VALUES ({placeholders})",
                [payload[c] for c in columns],
            )
            supplier_id = cur.lastrowid
            logger.info("Created golden record #%s: %s", supplier_id, supplier_data.get("canonical_name"))
            return supplier_id

    def merge_into_golden(self, supplier_id: int, supplier_data: Dict[str, Any]) -> None:
        """Merge a newly-scraped record into an existing golden record.

        Merge strategy:
        - Scalar fields: only overwrite if the existing value is NULL/empty
          and the new value is present (never let a lower-confidence source
          clobber an already-known fact).
        - JSON array fields: union of existing + new values, deduplicated,
          so information accumulates across sources instead of being lost.
        - source_count is incremented and last_updated bumped.
        """
        existing = self.get_supplier(supplier_id)
        if existing is None:
            raise ValueError(f"No supplier with id={supplier_id} to merge into")

        incoming = self._prepare_supplier_payload(supplier_data)
        updates: Dict[str, Any] = {}

        for field, new_value in incoming.items():
            if field in SUPPLIER_JSON_FIELDS:
                existing_raw = existing.get(field)
                existing_list = existing_raw if isinstance(existing_raw, list) else []
                try:
                    new_list = json.loads(new_value) if isinstance(new_value, str) else new_value
                except (json.JSONDecodeError, TypeError):
                    new_list = []
                if not isinstance(new_list, list):
                    new_list = [new_list]
                merged = existing_list + [v for v in new_list if v not in existing_list]
                if merged != existing_list:
                    updates[field] = json.dumps(merged, default=_json_default)
            else:
                existing_value = existing.get(field)
                if existing_value in (None, "", 0) and new_value not in (None, ""):
                    updates[field] = new_value

        updates["source_count"] = (existing.get("source_count") or 1) + 1
        updates["last_updated"] = datetime.now(timezone.utc).isoformat()

        set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
        with connection_scope(self.db_path) as conn:
            conn.execute(
                f"UPDATE suppliers SET {set_clause} WHERE id = ?",
                [*updates.values(), supplier_id],
            )
        logger.info("Merged data into golden record #%s (%d fields updated)", supplier_id, len(updates))

    def update_supplier_fields(self, supplier_id: int, fields: Dict[str, Any]) -> None:
        """Direct field update for arbitrary writable columns — used by
        verification and scoring steps that aren't a "merge" so much as
        an authoritative overwrite (e.g. Qichacha confirms USCC status)."""
        payload = self._prepare_supplier_payload(fields)
        if not payload:
            return
        payload["last_updated"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{col} = ?" for col in payload.keys())
        with connection_scope(self.db_path) as conn:
            conn.execute(
                f"UPDATE suppliers SET {set_clause} WHERE id = ?",
                [*payload.values(), supplier_id],
            )

    def update_supplier_fields_with_history(
        self, supplier_id: int, fields: Dict[str, Any], *, changed_by: str, change_reason: Optional[str] = None,
    ) -> List[tuple]:
        """Same authoritative-overwrite semantics as `update_supplier_fields`,
        but diffs against the current row first and writes one
        `supplier_change_log` row per field that actually changed --
        the mechanism behind "update existing records when reverified,
        never blindly overwrite" for verification_ai/ and collection/.
        Calls the existing `update_supplier_fields` for the actual
        write rather than duplicating the SQL. `changed_by` should be
        one of 'collection_service' | 'verification_service' |
        'discovery_service' | 'merge' | 'manual', matching
        supplier_change_log.changed_by's documented (but deliberately
        unconstrained -- see MIGRATIONS[11]) convention.

        Returns the list of (field_name, old_value, new_value) tuples
        actually written to the change log, for callers that want to
        report what changed without a second query.
        """
        existing = self.get_supplier(supplier_id)
        if existing is None:
            raise ValueError(f"No supplier with id={supplier_id} to update")

        payload = self._prepare_supplier_payload(fields)
        changes: List[tuple] = []
        for field, new_value in payload.items():
            old_value = existing.get(field)
            old_comparable = json.dumps(old_value, default=_json_default) if isinstance(old_value, (list, dict)) else old_value
            if old_comparable != new_value:
                changes.append((field, old_comparable, new_value))

        self.update_supplier_fields(supplier_id, fields)

        if changes:
            with connection_scope(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO supplier_change_log "
                    "(supplier_id, field_name, old_value, new_value, changed_by, change_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (supplier_id, field, old_val, new_val, changed_by, change_reason)
                        for field, old_val, new_val in changes
                    ],
                )
        return changes

    def get_supplier_change_log(self, supplier_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_change_log WHERE supplier_id = ? "
                "ORDER BY changed_at DESC, id DESC LIMIT ?",
                (supplier_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_supplier(self, supplier_id: int) -> None:
        """Hard delete. Cascades to shipment_records and dedup_candidates
        via ON DELETE CASCADE/SET NULL foreign keys."""
        with connection_scope(self.db_path) as conn:
            conn.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))

    # ═════════════════════════════════════════════════════
    # CERTIFICATIONS
    # ═════════════════════════════════════════════════════

    # Whitelisted so a caller can never inject an arbitrary column name
    # into the f-string below.
    CERT_FLAG_FIELDS: Sequence[str] = (
        "iso_9001", "iso_ts_16949", "e_mark_certified",
        "ce_certified", "ukca_certified", "iatf_16949",
    )

    def get_suppliers_by_cert_flag(
        self, field_name: str, value: bool = True, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Suppliers where a given certification boolean column matches
        `value`. Used by verification.cert_checker to find suppliers
        whose certificates need an expiry check."""
        if field_name not in self.CERT_FLAG_FIELDS:
            raise ValueError(
                f"Unsupported cert field '{field_name}'. Must be one of: {self.CERT_FLAG_FIELDS}"
            )
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM suppliers WHERE {field_name} = ? LIMIT ?",
                (1 if value else 0, limit),
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    # ═════════════════════════════════════════════════════
    # VERIFICATION
    # ═════════════════════════════════════════════════════

    def get_unverified_chinese(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Suppliers based in China with a USCC on file that hasn't yet
        been confirmed against the Qichacha registry."""
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM suppliers
                WHERE country = 'China'
                  AND uscc IS NOT NULL
                  AND uscc != ''
                  AND uscc_verified = 0
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def update_verification(self, supplier_id: int, verification: Dict[str, Any]) -> None:
        """Record the outcome of a Qichacha (or other registry) check.
        Expected keys in `verification`: uscc_verified (bool), plus any
        of the other writable supplier fields the verifier confirmed
        (company_reg_number, year_established, is_manufacturer,
        business_scope, registered_capital_rmb, etc.)."""
        fields = dict(verification)
        fields["uscc_verified"] = bool(fields.get("uscc_verified", False))
        fields["uscc_verified_at"] = datetime.now(timezone.utc).isoformat()
        self.update_supplier_fields(supplier_id, fields)

    # ═════════════════════════════════════════════════════
    # MANUFACTURER VERIFICATION
    # ═════════════════════════════════════════════════════

    def get_suppliers_needing_manufacturer_assessment(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers that verification.manufacturer_verifier.ManufacturerVerifier
        hasn't assessed yet (manufacturer_verified_at is still NULL).

        Known limitation: once a supplier IS assessed, it's excluded
        here permanently — even if better inputs (e.g. business_scope
        from a later Qichacha verification) arrive afterwards. Within a
        single pipeline run this is a non-issue (verification runs
        before manufacturer assessment, so same-run data is already
        available), but a supplier assessed sparsely in an early run
        won't automatically get reassessed once richer data shows up
        later. Pass force=True to re-assess every supplier regardless of
        manufacturer_verified_at — e.g. after a batch Qichacha backfill,
        or after tuning verification.manufacturer_verifier's thresholds."""
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute("SELECT * FROM suppliers LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE manufacturer_verified_at IS NULL
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def update_manufacturer_verification(self, supplier_id: int, result: Dict[str, Any]) -> None:
        """Persist the output of ManufacturerVerifier.assess(). Expected
        keys: manufacturer_confidence (int), is_manufacturer (bool or
        None), manufacturer_signals (list of strings).

        Note on None: is_manufacturer=None ("genuinely inconclusive") is
        passed straight to update_supplier_fields, which — like every
        other field — skips writing values that are None. That's the
        right behaviour here: an inconclusive re-assessment shouldn't
        erase a previous (weaker but non-null) guess from a normalizer;
        it just means this pass didn't have enough to override it."""
        fields = dict(result)
        fields["manufacturer_verified_at"] = datetime.now(timezone.utc).isoformat()
        self.update_supplier_fields(supplier_id, fields)

    def update_factory_photo_assessment(
        self, supplier_id: int, verdict: str, reasoning: Optional[str] = None
    ) -> None:
        """Persist the output of verification.factory_photo_verifier.FactoryPhotoVerifier."""
        fields = {"factory_photo_verdict": verdict}
        if reasoning:
            existing = self.get_supplier(supplier_id) or {}
            existing_signals = existing.get("manufacturer_signals") or []
            fields["manufacturer_signals"] = existing_signals + [f"Factory photo assessment: {reasoning}"]
        fields["factory_photo_assessed_at"] = datetime.now(timezone.utc).isoformat()
        self.update_supplier_fields(supplier_id, fields)

    # ═════════════════════════════════════════════════════
    # SCORING
    # ═════════════════════════════════════════════════════

    def get_unscored(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Suppliers that have never been scored (composite_score still
        at its default of 0) or were updated more recently than they
        were last scored."""
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM suppliers
                WHERE composite_score = 0
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def get_sources_by_supplier(self, supplier_ids: Sequence[int]) -> Dict[int, set]:
        """Batch-fetch which raw_source_data.source values fed each
        golden record, for verification.scorer.SupplierScorer's
        provenance dimension (source quality + independent-source
        corroboration). One grouped query rather than one per supplier
        -- this is meant to back a full-database rescore."""
        if not supplier_ids:
            return {}
        with connection_scope(self.db_path) as conn:
            placeholders = ", ".join("?" for _ in supplier_ids)
            rows = conn.execute(
                f"""
                SELECT golden_record_id, source FROM raw_source_data
                WHERE golden_record_id IN ({placeholders})
                """,
                list(supplier_ids),
            ).fetchall()
        result: Dict[int, set] = {sid: set() for sid in supplier_ids}
        for row in rows:
            result[row["golden_record_id"]].add(row["source"])
        return result

    def get_capability_findings_by_supplier(
        self, supplier_ids: Sequence[int]
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Batch-fetch supplier_capabilities rows for
        verification.scorer.SupplierScorer's self-asserted-verification
        bonus -- a capability finding scraped from a supplier's own
        website is a claim, not independent verification (see
        _verification_score's own docstring), so this is kept as its
        own signal rather than blended into the hard-evidence
        verification_score. One grouped query rather than one per
        supplier, mirroring get_sources_by_supplier's exact pattern --
        this is meant to back a full-database rescore."""
        if not supplier_ids:
            return {}
        with connection_scope(self.db_path) as conn:
            placeholders = ", ".join("?" for _ in supplier_ids)
            rows = conn.execute(
                f"""
                SELECT supplier_id, category, relationship, confidence
                FROM supplier_capabilities
                WHERE supplier_id IN ({placeholders})
                """,
                list(supplier_ids),
            ).fetchall()
        result: Dict[int, List[Dict[str, Any]]] = {sid: [] for sid in supplier_ids}
        for row in rows:
            result[row["supplier_id"]].append(dict(row))
        return result

    def update_scores(self, supplier_id: int, scores: Dict[str, Any]) -> None:
        """Persist the output of SupplierScorer.score(). Only the five
        known score fields + recommendation are written."""
        payload = {k: v for k, v in scores.items() if k in SCORE_FIELDS}
        if not payload:
            return
        payload["last_updated"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{col} = ?" for col in payload.keys())
        with connection_scope(self.db_path) as conn:
            conn.execute(
                f"UPDATE suppliers SET {set_clause} WHERE id = ?",
                [*payload.values(), supplier_id],
            )

    # ═════════════════════════════════════════════════════
    # DEDUPLICATION REVIEW QUEUE
    # ═════════════════════════════════════════════════════

    def add_to_review_queue(
        self,
        supplier_id_a: int,
        supplier_id_b: int,
        match_score: float,
        match_signals: Optional[Dict[str, Any]] = None,
    ) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO dedup_candidates (supplier_id_a, supplier_id_b, match_score, match_signals)
                VALUES (?, ?, ?, ?)
                """,
                (supplier_id_a, supplier_id_b, match_score, json.dumps(match_signals or {})),
            )
            return cur.lastrowid

    def get_pending_review_candidates(self, limit: int = 100) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM dedup_candidates
                WHERE status = 'pending'
                ORDER BY match_score DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return _rows_to_dicts(rows, ("match_signals",))

    def get_review_candidate(self, candidate_id: int) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM dedup_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return _row_to_dict(row, ("match_signals",))

    def resolve_review_candidate(self, candidate_id: int, status: str) -> None:
        """status must be 'merged' or 'rejected'."""
        if status not in ("merged", "rejected"):
            raise ValueError("status must be 'merged' or 'rejected'")
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                UPDATE dedup_candidates
                SET status = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (status, datetime.now(timezone.utc).isoformat(), candidate_id),
            )

    def merge_two_suppliers(self, keep_id: int, remove_id: int) -> None:
        """Merge remove_id's data into keep_id (reusing merge_into_golden's
        non-clobbering-scalar / union-JSON-array logic), reassign its
        shipment records, raw-source links, and any other pending
        dedup_candidates rows that reference it, then delete remove_id.

        This is what actually closes Phase 1's Gap 6 — until this
        existed, a 'review_queued' match created two permanent golden
        records with no way to ever combine them back into one, even
        after a human confirmed they were the same company.

        Safety note: merge_into_golden's non-clobbering-scalar rule means
        if both suppliers have a non-null uscc, keep_id's own value is
        always preserved and remove_id's is silently discarded (harmless,
        since that record is deleted right after) — this can never raise
        a UNIQUE-constraint IntegrityError on uscc, because keep_id's
        differing value is never written over it. It also means a merge
        can silently keep the "wrong" uscc if the two records really
        were different legal entities incorrectly matched — this is why
        `main.py review list` shows both records' uscc side by side
        before a human confirms the merge, rather than merging blind.
        """
        if keep_id == remove_id:
            raise ValueError("keep_id and remove_id must refer to different suppliers")

        remove_supplier = self.get_supplier(remove_id)
        if remove_supplier is None:
            raise ValueError(f"No supplier with id={remove_id} to merge from")
        if self.get_supplier(keep_id) is None:
            raise ValueError(f"No supplier with id={keep_id} to merge into")

        # Reuse the existing non-clobbering merge logic by feeding
        # remove_supplier's full record in as if it were freshly-scraped
        # candidate data.
        self.merge_into_golden(keep_id, remove_supplier)

        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE shipment_records SET supplier_id = ? WHERE supplier_id = ?",
                (keep_id, remove_id),
            )
            conn.execute(
                "UPDATE raw_source_data SET golden_record_id = ? WHERE golden_record_id = ?",
                (keep_id, remove_id),
            )
            conn.execute(
                "UPDATE dedup_candidates SET supplier_id_a = ? WHERE supplier_id_a = ?",
                (keep_id, remove_id),
            )
            conn.execute(
                "UPDATE dedup_candidates SET supplier_id_b = ? WHERE supplier_id_b = ?",
                (keep_id, remove_id),
            )
            conn.execute("DELETE FROM suppliers WHERE id = ?", (remove_id,))

        logger.info("Merged supplier #%s into #%s and removed the duplicate", remove_id, keep_id)

    def resolve_review_candidate_as_merge(self, candidate_id: int, keep: str = "b") -> int:
        """Action a pending dedup_candidates row as a confirmed duplicate:
        merge the two supplier records into one and mark the candidate
        'merged'. `keep` selects which side survives — 'a'
        (supplier_id_a, the newly-scraped record) or 'b' (supplier_id_b,
        the default — the pre-existing record it was matched against;
        see deduplication.matcher.SupplierMatcher.resolve_and_store).
        Returns the id of the supplier that survived."""
        if keep not in ("a", "b"):
            raise ValueError("keep must be 'a' or 'b'")

        candidate = self.get_review_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"No dedup_candidates row with id={candidate_id}")
        if candidate["status"] != "pending":
            raise ValueError(f"dedup_candidates #{candidate_id} is already '{candidate['status']}', not pending")

        id_a, id_b = candidate["supplier_id_a"], candidate["supplier_id_b"]
        keep_id, remove_id = (id_a, id_b) if keep == "a" else (id_b, id_a)

        self.merge_two_suppliers(keep_id, remove_id)
        self.resolve_review_candidate(candidate_id, "merged")
        return keep_id

    def resolve_review_candidate_as_reject(self, candidate_id: int) -> None:
        """Action a pending dedup_candidates row as 'these are genuinely
        different suppliers' — both records are left untouched, only
        the candidate's status changes."""
        candidate = self.get_review_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"No dedup_candidates row with id={candidate_id}")
        if candidate["status"] != "pending":
            raise ValueError(f"dedup_candidates #{candidate_id} is already '{candidate['status']}', not pending")
        self.resolve_review_candidate(candidate_id, "rejected")

    # ═════════════════════════════════════════════════════
    # SHIPMENT RECORDS
    # ═════════════════════════════════════════════════════

    def add_shipment_record(self, record: Dict[str, Any]) -> int:
        columns = (
            "supplier_id", "source", "shipper_name", "consignee_name",
            "consignee_country", "shipment_date", "hs_code", "product_desc",
            "quantity", "weight_kg", "value_usd", "origin_port",
            "destination_port", "raw_record",
        )
        payload = {c: record.get(c) for c in columns if c in record}
        if "raw_record" in payload and not isinstance(payload["raw_record"], str):
            payload["raw_record"] = json.dumps(payload["raw_record"], default=_json_default)

        col_sql = ", ".join(payload.keys())
        placeholders = ", ".join("?" for _ in payload)
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                f"INSERT INTO shipment_records ({col_sql}) VALUES ({placeholders})",
                list(payload.values()),
            )
            return cur.lastrowid

    def get_shipments_for_supplier(self, supplier_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM shipment_records
                WHERE supplier_id = ?
                ORDER BY shipment_date DESC
                LIMIT ?
                """,
                (supplier_id, limit),
            ).fetchall()
            return _rows_to_dicts(rows)

    # ═════════════════════════════════════════════════════
    # SEARCH LOG
    # ═════════════════════════════════════════════════════

    def log_search(
        self,
        query: str,
        category: Optional[str] = None,
        sources_used: Optional[List[str]] = None,
        results_count: int = 0,
        user_feedback: Optional[str] = None,
    ) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO search_log (query, category, sources_used, results_count, user_feedback)
                VALUES (?, ?, ?, ?, ?)
                """,
                (query, category, json.dumps(sources_used or []), results_count, user_feedback),
            )
            return cur.lastrowid

    def recent_searches(self, limit: int = 50) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM search_log ORDER BY searched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return _rows_to_dicts(rows, ("sources_used",))

    # ═════════════════════════════════════════════════════
    # INCREMENTAL SCRAPING (addresses Phase 1 Gap 4)
    # ═════════════════════════════════════════════════════
    #
    # search_log records one row per *pipeline run* across possibly
    # several sources at once, which makes it awkward to answer "was
    # this specific (source, query) pair scraped recently" without
    # parsing its sources_used JSON on every row. source_query_runs
    # exists purely to make that one query fast and simple.

    def record_source_query_run(self, source: str, query: str, results_count: int = 0) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO source_query_runs (source, query, results_count)
                VALUES (?, ?, ?)
                """,
                (source, query, results_count),
            )
            return cur.lastrowid

    def get_last_source_query_run(self, source: str, query: str) -> Optional[Dict[str, Any]]:
        """Most recent run for this exact (source, query) pair, or None
        if it's never been scraped. Matching is exact-string on `query`
        — a differently-worded search for the same product is treated
        as a distinct query, by design; normalising/fuzzy-matching
        query text is a reasonable future enhancement but isn't done here.

        Ordered by (run_at DESC, id DESC): SQLite's CURRENT_TIMESTAMP
        only has one-second resolution, so two runs recorded within the
        same second would tie on run_at alone — id, being monotonically
        increasing, reliably breaks that tie in favour of the actual
        most-recent insert."""
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM source_query_runs
                WHERE source = ? AND query = ?
                ORDER BY run_at DESC, id DESC
                LIMIT 1
                """,
                (source, query),
            ).fetchone()
            return _row_to_dict(row)

    def was_recently_scraped(self, source: str, query: str, within_days: int) -> bool:
        """True if (source, query) was scraped within the last
        `within_days` days. Used by the pipeline to skip a scrape and
        save Apify credits / avoid unnecessary rate-limit exposure —
        pass within_days=0 to always return False (never skip)."""
        if within_days <= 0:
            return False
        last_run = self.get_last_source_query_run(source, query)
        if last_run is None:
            return False
        try:
            run_at = datetime.fromisoformat(last_run["run_at"])
        except (ValueError, TypeError):
            return False
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - run_at).total_seconds() / 86400
        return age_days < within_days

    # ═════════════════════════════════════════════════════
    # MANUFACTURING CAPABILITY FINDINGS (v5)
    # Populated by verification.capability_extractor.CapabilityExtractor
    # from a supplier's own website — see storage/database.py's v5
    # migration for the table shape.
    # ═════════════════════════════════════════════════════

    def add_capability_finding(self, supplier_id: int, finding: Dict[str, Any]) -> Optional[int]:
        """Insert one capability finding. Idempotent on
        (supplier_id, reported_term, relationship) via the table's own
        UNIQUE constraint — re-running extraction against a page that
        hasn't changed inserts nothing new. Returns the new row id, or
        `None` if this exact finding was already on file (distinct
        from an error — there is no error case here, `INSERT OR
        IGNORE` never raises for a uniqueness collision).

        `finding` is expected to have the same keys as
        `verification.capability_extractor.CapabilityFinding`'s
        fields: reported_term, canonical_term, category, relationship,
        confidence, evidence, source_url.
        """
        columns = (
            "supplier_id", "reported_term", "canonical_term", "category",
            "relationship", "confidence", "evidence", "source_url",
        )
        payload = {"supplier_id": supplier_id, **{c: finding.get(c) for c in columns if c != "supplier_id"}}
        col_sql = ", ".join(payload.keys())
        placeholders = ", ".join("?" for _ in payload)
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO supplier_capabilities ({col_sql}) VALUES ({placeholders})",
                list(payload.values()),
            )
            return cur.lastrowid if cur.rowcount > 0 else None

    def get_capabilities(self, supplier_id: int) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_capabilities WHERE supplier_id = ? ORDER BY assessed_at DESC",
                (supplier_id,),
            ).fetchall()
            return _rows_to_dicts(rows)

    def get_capabilities_missing_category(self) -> List[Dict[str, Any]]:
        """Every supplier_capabilities row with category IS NULL, across
        every supplier -- "unmapped" findings from a reported_term that
        wasn't in verification.capability_vocabulary.VOCABULARY at
        extraction time (see that module's own "stored as unmapped, not
        discarded" discipline). Some of these may be mappable now if the
        vocabulary has grown since -- see backfill_capability_category,
        the one-time correction this powers."""
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_capabilities WHERE category IS NULL ORDER BY supplier_id, assessed_at",
            ).fetchall()
            return _rows_to_dicts(rows)

    def backfill_capability_category(self, row_id: int, canonical_term: str, category: str) -> None:
        """Corrects a single supplier_capabilities row's canonical_term/
        category in place -- for a reported_term that wasn't recognised
        by the vocabulary at extraction time but is now. Unlike
        add_capability_finding, this UPDATEs an existing row rather than
        inserting a new one: the evidence/source_url/relationship this
        stage already captured stay exactly as extracted, only the
        classification catches up to the current vocabulary."""
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE supplier_capabilities SET canonical_term = ?, category = ? WHERE id = ?",
                (canonical_term, category, row_id),
            )

    # -- Audit verdicts (Stage 2 -- manual A/B/C/D/Qualified capture) ---

    _AUDIT_VERDICT_CRITERIA = ("A", "B", "C", "D", "Qualified", "Notes")
    _AUDIT_VERDICT_VALUES = {
        "A": ("Pending", "Pass", "Fail"),
        "B": ("Pending", "Pass", "Fail"),
        "C": ("Pending", "Pass", "Fail"),
        "D": ("Pending", "Pass", "Fail"),
        "Qualified": ("Pending", "Yes", "No"),
        "Notes": (),  # value is unused on the Notes row -- must be None
    }

    def upsert_audit_verdict(
        self, supplier_id: int, criterion: str, *, value: Optional[str] = None, notes: Optional[str] = None,
    ) -> None:
        """Writes (or overwrites) the one current row for
        (supplier_id, criterion) -- a real human selection made in the
        Audit tab UI, never computed or inferred (see CLAUDE.md
        standing rule 2). `criterion` is one of _AUDIT_VERDICT_CRITERIA
        ('A'/'B'/'C'/'D'/'Qualified' for a verdict value, 'Notes' for
        the tracker's single shared free-text field -- see
        batch/tracker_exporter.py's own module docstring for why Notes
        isn't per-criterion). No CHECK constraint backs this (same fix
        as v4/v28) -- validated here in Python instead, including
        which values are legal for which criterion (Pending/Pass/Fail
        for A-D, Pending/Yes/No for Qualified, no value at all for
        Notes) -- this is an HTTP-facing boundary, not just an
        internal call, so a malformed request is rejected here rather
        than silently stored."""
        if criterion not in self._AUDIT_VERDICT_CRITERIA:
            raise ValueError(f"unknown audit verdict criterion: {criterion!r}")
        allowed_values = self._AUDIT_VERDICT_VALUES[criterion]
        if value is not None and value not in allowed_values:
            raise ValueError(f"invalid value {value!r} for criterion {criterion!r}, expected one of {allowed_values or '(no value at all)'}")
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO audit_verdicts (supplier_id, criterion, value, notes, set_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (supplier_id, criterion)
                DO UPDATE SET value = excluded.value, notes = excluded.notes, set_at = excluded.set_at
                """,
                (supplier_id, criterion, value, notes),
            )

    def get_audit_verdicts(self, supplier_id: int) -> Dict[str, Dict[str, Any]]:
        """{criterion: {value, notes, set_at}} for every row this
        supplier has -- a criterion with no row yet simply isn't a key
        in the returned dict; callers treat that as "Pending" (see
        batch/tracker_exporter.py's _verdict_or_pending)."""
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT criterion, value, notes, set_at FROM audit_verdicts WHERE supplier_id = ?",
                (supplier_id,),
            ).fetchall()
            return {row["criterion"]: dict(row) for row in rows}

    def get_audit_review_date(self, supplier_id: int) -> Optional[str]:
        """The date (YYYY-MM-DD, no time) of the most recent A/B/C/D/
        Qualified verdict set for this supplier -- deliberately
        excludes the 'Notes' row: typing a note alone doesn't count as
        "reviewed" for the tracker's own Date Reviewed column. None if
        no verdict has ever been set."""
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(set_at) AS latest FROM audit_verdicts "
                "WHERE supplier_id = ? AND criterion IN ('A', 'B', 'C', 'D', 'Qualified')",
                (supplier_id,),
            ).fetchone()
            latest = row["latest"] if row else None
            return latest[:10] if latest else None

    def clear_capabilities(self, supplier_id: int) -> int:
        """Deletes every existing capability finding for a supplier.

        Exists specifically because `add_capability_finding`'s own
        `INSERT OR IGNORE` uniqueness (on supplier_id, reported_term,
        relationship) means re-running extraction over a supplier
        never removes or updates a previous finding — only adds a new
        one if the (term, relationship) pair genuinely differs. Found
        from a real, confusing case: after fixing a prompt so it no
        longer claims a certification from vague evidence, re-running
        `extract-capabilities --force` still showed the old, wrong
        finding, because nothing had ever deleted it -- the fix was
        working, but there was no way to see that without first
        clearing the stale row. `--force` calls this before
        re-extracting for exactly that reason.

        Returns the number of rows deleted.
        """
        with connection_scope(self.db_path) as conn:
            cur = conn.execute("DELETE FROM supplier_capabilities WHERE supplier_id = ?", (supplier_id,))
            return cur.rowcount

    def find_suppliers_by_capability(
        self,
        canonical_term: str,
        relationship: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """The buyer-facing query this table exists for: which
        suppliers has capability extraction found evidence of
        `canonical_term` for. Joins back to the full `suppliers` row
        (not just the capability row) so a caller gets a normal,
        complete supplier record — the same shape every other find_*
        method returns — with capability evidence available separately
        via `get_capabilities` if needed.

        `relationship=None` (the default) matches either 'in_house' or
        'subcontracted' — a rotomoulder who subcontracts injection
        moulding is still a legitimate answer to a query that needs
        both processes; pass relationship='in_house' explicitly to
        require the supplier operate it themselves.
        """
        sql = """
            SELECT DISTINCT s.* FROM suppliers s
            JOIN supplier_capabilities c ON c.supplier_id = s.id
            WHERE c.canonical_term = ? AND c.confidence >= ?
        """
        params: List[Any] = [canonical_term, min_confidence]
        if relationship is not None:
            sql += " AND c.relationship = ?"
            params.append(relationship)
        sql += " ORDER BY s.composite_score DESC LIMIT ?"
        params.append(limit)

        with connection_scope(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def mark_capability_extraction_attempted(self, supplier_id: int, status: str = "extracted") -> None:
        """Record that capability extraction ran for this supplier,
        regardless of how many findings (if any) it produced. Call
        this once per supplier per extraction pass, alongside — not
        instead of — `add_capability_finding` for each individual
        finding. Without this, a supplier whose website genuinely has
        no capability language would look identical to one that has
        never been attempted, and `get_suppliers_needing_capability_extraction`
        would re-attempt it forever.

        `status` is "extracted" (the page was actually read, whether or
        not it yielded findings) or "fetch_failed" (couldn't reach the
        site at all this attempt) -- see `capability_extraction_status`
        column comment. Callers should pass "fetch_failed" specifically
        when `own_website_scraper.fetch()` itself failed, so zero
        findings from an unreachable site isn't displayed the same as
        zero findings from a site that was successfully read.
        """
        self.update_supplier_fields(
            supplier_id, {
                "capability_extracted_at": datetime.now(timezone.utc).isoformat(),
                "capability_extraction_status": status,
            }
        )

    def add_catalogue_signal_finding(self, supplier_id: int, finding: Dict[str, Any]) -> Optional[int]:
        """Insert one catalogue-depth signal finding. Idempotent on
        (supplier_id, signal_type, evidence) via the table's own UNIQUE
        constraint -- re-running extraction against a page that hasn't
        changed inserts nothing new. Returns the new row id, or `None`
        if this exact finding was already on file (not an error --
        `INSERT OR IGNORE` never raises for a uniqueness collision).

        `finding` is expected to have the same keys as
        `verification.catalogue_depth_extractor.CatalogueSignalFinding`'s
        fields: signal_type, evidence, source_url.
        """
        columns = ("supplier_id", "signal_type", "evidence", "source_url")
        payload = {"supplier_id": supplier_id, **{c: finding.get(c) for c in columns if c != "supplier_id"}}
        col_sql = ", ".join(payload.keys())
        placeholders = ", ".join("?" for _ in payload)
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO supplier_catalogue_signals ({col_sql}) VALUES ({placeholders})",
                list(payload.values()),
            )
            return cur.lastrowid if cur.rowcount > 0 else None

    def get_catalogue_signals(self, supplier_id: int) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_catalogue_signals WHERE supplier_id = ? ORDER BY assessed_at DESC",
                (supplier_id,),
            ).fetchall()
            return _rows_to_dicts(rows)

    def mark_catalogue_depth_extraction_attempted(self, supplier_id: int) -> None:
        """Same never-attempted-vs-attempted-and-found-nothing discipline
        as `mark_capability_extraction_attempted` -- call once per
        supplier per extraction pass regardless of outcome (no
        collection artifacts on disk, a fetch/parse failure, or zero
        signals found are all real, durable facts, not reasons to
        retry forever)."""
        self.update_supplier_fields(
            supplier_id, {"catalogue_depth_extracted_at": datetime.now(timezone.utc).isoformat()}
        )

    def get_suppliers_needing_catalogue_depth_extraction(
        self, limit: int = 20, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers that haven't had catalogue-depth extraction
        attempted yet. Mirrors `get_suppliers_needing_capability_extraction`'s
        own timestamp-gated pattern exactly -- `catalogue_depth_extracted_at
        IS NULL` is "never attempted," not "attempted, no collection
        artifacts available." Not filtered on `domain IS NOT NULL` here
        (unlike the capability-extraction query) since availability for
        this stage depends on a prior successful collection_runs row,
        not just a domain -- checked by the caller
        (CatalogueDepthService), which marks attempted either way so a
        supplier that never went through collection isn't re-checked
        forever."""
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute("SELECT * FROM suppliers LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE catalogue_depth_extracted_at IS NULL LIMIT ?",
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def enrich_contact_details(
        self, supplier_id: int, *, emails: List[str], phones: List[str],
        contact_form_url: Optional[str] = None,
        whatsapp: Optional[str] = None, wechat_id: Optional[str] = None,
        source_pages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Fold website-discovered contact details into a supplier
        record, filling gaps only — never overwriting a value already
        on file. This is a deliberately different write discipline
        from `update_supplier_fields`'s usual "authoritative overwrite"
        role (see that method's own docstring): a company's own website
        is *a* source of contact info, not necessarily a better one
        than whatever an Alibaba/IndiaMart listing already provided, so
        it only fills what's missing rather than replacing what's
        there.

        `primary_email`/`primary_phone` are set only if currently
        empty. Every email beyond the first (or every email at all, if
        `primary_email` was already set) is added to `secondary_emails`,
        deduplicated against what's already there.

        `phones` is still used only for `primary_phone` (first found,
        gap-fill only) — the FULL set of typed phone numbers is a
        separate concern, see `save_phone_numbers` (a supplier can have
        several real numbers -- landline, mobile, WhatsApp, WeChat-
        linked, fax -- which don't fit a single scalar column; this
        method's job stays "what goes directly on the suppliers row").

        `whatsapp`/`wechat_id`: gap-fill only, same discipline as
        primary_email/primary_phone -- pass the caller's own
        context-detected WhatsApp number / WeChat ID (see
        `verification.website_contact_extractor`'s phone-type
        classification), never inferred here from a generic phone
        number alone.

        `source_pages`: unlike the gap-fill fields above, this
        ACCUMULATES -- every distinct page URL that contributed any
        contact finding is unioned into `contact_source_pages`,
        deduplicated, never replaced, matching the same
        "evidence accumulates across sources" JSON-array-merge
        philosophy `merge_into_golden` already uses. Row-level (which
        pages contributed something), not per-value -- per-value
        source lives on `supplier_phone_numbers.source_url` /
        `field_provenance`.

        `contact_form_url` is filled only when the supplier still has
        no `primary_email` after this call and no `contact_form_url`
        already on file — it's the fallback of last resort, not
        something that should ever displace a real email address, and
        a caller should pass `best_contact_method`'s form-URL result
        here specifically only when that function's own priority order
        (email, then phone, then form) already decided nothing better
        was found.

        Returns what was actually written (for pipeline stats), not
        everything that was found — a caller wanting the raw findings
        already has them from the extractor.
        """
        existing = self.get_supplier(supplier_id)
        if existing is None:
            return {
                "primary_email_set": False, "secondary_emails_added": 0,
                "primary_phone_set": False, "contact_form_url_set": False,
                "whatsapp_set": False, "wechat_id_set": False, "source_pages_added": 0,
            }

        fields: Dict[str, Any] = {}
        result = {
            "primary_email_set": False, "secondary_emails_added": 0,
            "primary_phone_set": False, "contact_form_url_set": False,
            "whatsapp_set": False, "wechat_id_set": False, "source_pages_added": 0,
        }

        remaining_emails = list(emails)
        if not existing.get("primary_email") and remaining_emails:
            fields["primary_email"] = remaining_emails[0]
            remaining_emails = remaining_emails[1:]
            result["primary_email_set"] = True

        if remaining_emails:
            existing_secondary = existing.get("secondary_emails") or []
            new_secondary = list(existing_secondary)
            added = 0
            for email in remaining_emails:
                if email not in new_secondary and email != fields.get("primary_email"):
                    new_secondary.append(email)
                    added += 1
            if added:
                fields["secondary_emails"] = new_secondary
                result["secondary_emails_added"] = added

        if not existing.get("primary_phone") and phones:
            fields["primary_phone"] = phones[0]
            result["primary_phone_set"] = True

        if not existing.get("whatsapp") and whatsapp:
            fields["whatsapp"] = whatsapp
            result["whatsapp_set"] = True

        if not existing.get("wechat_id") and wechat_id:
            fields["wechat_id"] = wechat_id
            result["wechat_id_set"] = True

        if source_pages:
            existing_pages = existing.get("contact_source_pages") or []
            new_pages = list(existing_pages)
            added = 0
            for url in source_pages:
                if url and url not in new_pages:
                    new_pages.append(url)
                    added += 1
            if added:
                fields["contact_source_pages"] = new_pages
                result["source_pages_added"] = added

        has_email_now = bool(existing.get("primary_email")) or result["primary_email_set"]
        if contact_form_url and not has_email_now and not existing.get("contact_form_url"):
            fields["contact_form_url"] = contact_form_url
            result["contact_form_url_set"] = True

        if fields:
            self.update_supplier_fields(supplier_id, fields)

        return result

    def save_phone_numbers(self, supplier_id: int, phones: List[Dict[str, Any]]) -> int:
        """Insert-or-ignore per (supplier_id, phone_number, phone_type)
        -- see supplier_phone_numbers' own SCHEMA_SQL comment for why
        this is a dedicated table rather than a scalar/JSON column: a
        supplier can have several real numbers, each independently
        worth keeping, unlike primary_phone's one-value-only shape.
        `phones` is a list of {"phone_number", "phone_type",
        "source_url"} dicts. Returns how many rows were newly inserted
        (for pipeline stats) -- a repeat collection run re-finding the
        same (number, type) pair is a no-op, not an error."""
        if not phones:
            return 0
        inserted = 0
        with connection_scope(self.db_path) as conn:
            for phone in phones:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO supplier_phone_numbers
                        (supplier_id, phone_number, phone_type, source_url)
                    VALUES (?, ?, ?, ?)
                    """,
                    (supplier_id, phone["phone_number"], phone["phone_type"], phone.get("source_url")),
                )
                if cur.rowcount:
                    inserted += 1
        return inserted

    def get_phone_numbers(self, supplier_id: int) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_phone_numbers WHERE supplier_id = ? ORDER BY extracted_at",
                (supplier_id,),
            ).fetchall()
            return _rows_to_dicts(rows)

    def save_reputation_snippets(self, supplier_id: int, snippets: List[Dict[str, Any]]) -> int:
        """Insert-or-ignore per (supplier_id, query_type, link) -- see
        supplier_reputation_snippets' own SCHEMA_SQL comment. `snippets`
        is a list of {"query_type", "query_text", "title", "link",
        "snippet"} dicts, straight from GoogleSearchScraper's raw_data --
        no Clean/Flagged judgment of any kind is computed or stored
        here, only what the search engine returned. Returns how many
        rows were newly inserted (a repeat search re-finding the same
        (query_type, link) pair is a no-op, not an error)."""
        if not snippets:
            return 0
        inserted = 0
        with connection_scope(self.db_path) as conn:
            for s in snippets:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO supplier_reputation_snippets
                        (supplier_id, query_type, query_text, title, link, snippet)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (supplier_id, s["query_type"], s["query_text"], s.get("title"), s.get("link"), s.get("snippet")),
                )
                if cur.rowcount:
                    inserted += 1
        return inserted

    def mark_reputation_search_attempted(self, supplier_id: int) -> None:
        """Same never-attempted-vs-attempted-and-found-nothing discipline as
        `mark_capability_extraction_attempted`/`mark_catalogue_depth_extraction_attempted`
        -- call once per supplier per `_attempt_reputation_search` call
        regardless of outcome (no usable name yet, every query errored, or
        every query returned nothing are all real, durable facts, not
        reasons to display "not run" for a supplier that genuinely was
        searched and turned up nothing)."""
        self.update_supplier_fields(
            supplier_id, {"reputation_search_attempted_at": datetime.now(timezone.utc).isoformat()}
        )

    def get_reputation_snippets(self, supplier_id: int, query_type: Optional[str] = None) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            if query_type:
                rows = conn.execute(
                    "SELECT * FROM supplier_reputation_snippets WHERE supplier_id = ? AND query_type = ? "
                    "ORDER BY searched_at",
                    (supplier_id, query_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM supplier_reputation_snippets WHERE supplier_id = ? ORDER BY searched_at",
                    (supplier_id,),
                ).fetchall()
            return _rows_to_dicts(rows)

    def record_factory_photo_assessment(
        self, supplier_id: int, *, photo_urls: List[str], verdict: str
    ) -> None:
        """Writes into `factory_photo_urls`/`factory_photo_verdict`/
        `factory_photo_assessed_at` -- columns that have existed since
        Phase 2 but had no caller until
        `verification.factory_photo_verifier` was actually wired into
        the pipeline. `verdict` is one of
        `factory_photo_verifier.VALID_VERDICTS`
        ('plausible_factory' | 'implausible' | 'uncertain').
        """
        self.update_supplier_fields(supplier_id, {
            "factory_photo_urls": photo_urls,
            "factory_photo_verdict": verdict,
            "factory_photo_assessed_at": datetime.now(timezone.utc).isoformat(),
        })

    def get_suppliers_needing_facility_address_verification(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers with an address on file that haven't had
        verification.facility_address_verifier attempt to confirm it
        yet. Same never-attempted-vs-attempted-and-failed discipline
        as get_suppliers_needing_capability_extraction -- an address
        that genuinely doesn't resolve must not be re-checked (and
        re-billed) on every run forever.
        """
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE address IS NOT NULL AND address != '' LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE address IS NOT NULL AND address != ''
                    AND facility_address_verified_at IS NULL
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def mark_facility_address_verification_attempted(
        self, supplier_id: int, *, verified: bool, source: str
    ) -> None:
        self.update_supplier_fields(supplier_id, {
            "facility_address_verified": verified,
            "facility_address_verification_source": source,
            "facility_address_verified_at": datetime.now(timezone.utc).isoformat(),
        })

    def get_suppliers_needing_linkedin_check(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers who haven't had verification.linkedin_presence
        check for a company page yet. Same discipline again: checked
        and found nothing is a different fact from never checked."""
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute("SELECT * FROM suppliers LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE linkedin_checked_at IS NULL LIMIT ?",
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def mark_linkedin_checked(self, supplier_id: int, *, linkedin_url: Optional[str] = None) -> None:
        fields = {"linkedin_checked_at": datetime.now(timezone.utc).isoformat()}
        if linkedin_url:
            fields["linkedin_url"] = linkedin_url
        self.update_supplier_fields(supplier_id, fields)

    def get_suppliers_needing_website_search(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers with no known domain who haven't had
        `CompanyWebsiteFinder` try to find one yet. Mirrors
        `get_suppliers_needing_capability_extraction`'s exact
        timestamp-gated pattern and the exact reasoning behind it:
        `website_search_attempted_at IS NULL` is "never searched," not
        "searched and found nothing" -- an unfindable company must not
        be re-searched (and re-billed via SerpAPI) on every run
        forever. Pass force=True to re-attempt every domain-less
        supplier regardless of when it was last tried.
        """
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE domain IS NULL OR domain = '' LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE (domain IS NULL OR domain = '')
                    AND website_search_attempted_at IS NULL
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def mark_website_search_attempted(self, supplier_id: int) -> None:
        """Record that a name-based website search was attempted for
        this supplier, regardless of whether it found and validated a
        domain. Call this once per supplier per search pass, alongside
        — not instead of — writing `domain` itself when a match is
        validated. See `get_suppliers_needing_website_search`'s
        docstring for why this timestamp exists at all: the same
        never-attempted-vs-attempted-and-failed distinction that
        `capability_extracted_at` already established for the
        capability-extraction stage, applied here so an unfindable
        company doesn't burn a paid SerpAPI call every single run.
        """
        self.update_supplier_fields(
            supplier_id, {"website_search_attempted_at": datetime.now(timezone.utc).isoformat()}
        )

    def get_suppliers_needing_capability_extraction(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers with a known domain that haven't had capability
        extraction attempted against their own website yet. Mirrors
        `get_suppliers_needing_manufacturer_assessment`'s own
        timestamp-gated pattern exactly:
        `capability_extracted_at IS NULL` is "never attempted," not
        "attempted and found nothing" — those are different facts, and
        conflating them (e.g. by keying on whether a
        `supplier_capabilities` row exists) would re-attempt a
        genuinely capability-less site on every single run forever.
        Pass force=True to re-run extraction across every supplier
        with a domain regardless of when it last ran — e.g. after
        extending `capability_vocabulary.VOCABULARY` and wanting
        previously-unmapped terms re-checked.
        """
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE domain IS NOT NULL AND domain != '' LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE domain IS NOT NULL AND domain != ''
                    AND capability_extracted_at IS NULL
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    # ═════════════════════════════════════════════════════
    # Async pipeline jobs (v8) -- backs the FastAPI layer's job-trigger
    # endpoint. Deliberately plain SQLite reads/writes, not a real
    # queue (Celery/Redis): for a single Railway instance running one
    # worker process, this is enough, and it avoids a whole new
    # infrastructure dependency for a first deployment. If this ever
    # needs to run across multiple instances, this table is the thing
    # to replace, not the API layer built on top of it.
    # ═════════════════════════════════════════════════════

    def create_pipeline_job(self, *, job_id: str, query: str, options: Dict[str, Any]) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "INSERT INTO pipeline_jobs (id, query, options, status) VALUES (?, ?, ?, 'queued')",
                (job_id, query, json.dumps(options)),
            )

    def mark_pipeline_job_running(self, job_id: str) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE pipeline_jobs SET status = 'running', started_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )

    def mark_pipeline_job_completed(self, job_id: str, *, stats: Dict[str, Any]) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE pipeline_jobs SET status = 'completed', stats = ?, completed_at = ? WHERE id = ?",
                (json.dumps(stats), datetime.now(timezone.utc).isoformat(), job_id),
            )

    def mark_pipeline_job_failed(self, job_id: str, *, error: str) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE pipeline_jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
                (error, datetime.now(timezone.utc).isoformat(), job_id),
            )

    def get_pipeline_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            job = dict(row)
            for field in ("options", "stats", "progress"):
                if job.get(field):
                    job[field] = json.loads(job[field])
            return job

    def list_pipeline_jobs(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            jobs = []
            for row in rows:
                job = dict(row)
                for field in ("options", "stats", "progress"):
                    if job.get(field):
                        job[field] = json.loads(job[field])
                jobs.append(job)
            return jobs

    def update_pipeline_job_progress(self, job_id: str, progress: Dict[str, Any]) -> None:
        """Live incremental status for a long-running job (currently only
        written by sourcing/sourcing_agent.py's per-candidate progress
        callback) -- polled by GET /pipeline/jobs/{id} the same way
        `stats` already is, just before the job actually completes."""
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE pipeline_jobs SET progress = ? WHERE id = ?",
                (json.dumps(progress, default=_json_default), job_id),
            )

    # ═════════════════════════════════════════════════════
    # CSV batch upload (v18) -- see batch/batch_service.py. batch_job_id
    # is a pipeline_jobs.id; row-level tracking lives here specifically
    # so the original spreadsheet columns survive untouched through to
    # export regardless of enrichment outcome, and so needs_url/
    # needs_name rows are queryable without parsing a JSON blob.
    # ═════════════════════════════════════════════════════

    def create_batch_upload_row(
        self, *, batch_job_id: str, row_index: int, original_columns: Dict[str, Any],
        company_name: Optional[str], name_source: str = "csv", website: Optional[str] = None,
        status: str = "pending",
    ) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO batch_upload_rows
                    (batch_job_id, row_index, original_columns, company_name, name_source, website, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (batch_job_id, row_index, json.dumps(original_columns, default=_json_default),
                 company_name, name_source, website, status),
            )
            return cur.lastrowid

    def update_batch_upload_row(self, row_id: int, fields: Dict[str, Any]) -> None:
        """Direct field update -- status/supplier_id/error_message/
        company_name (once a placeholder name is confirmed or replaced).
        No history tracking here (unlike suppliers'
        update_supplier_fields_with_history) -- a batch row is a
        one-shot record of what happened to this upload attempt, not a
        golden record accumulating corrections over time."""
        if not fields:
            return
        set_clause = ", ".join(f"{col} = ?" for col in fields.keys())
        with connection_scope(self.db_path) as conn:
            conn.execute(
                f"UPDATE batch_upload_rows SET {set_clause} WHERE id = ?",
                [*fields.values(), row_id],
            )

    def get_batch_upload_rows(self, batch_job_id: str) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM batch_upload_rows WHERE batch_job_id = ? ORDER BY row_index",
                (batch_job_id,),
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                if d.get("original_columns"):
                    d["original_columns"] = json.loads(d["original_columns"])
                results.append(d)
            return results

    # ═════════════════════════════════════════════════════
    # Field provenance (v18) -- source_url/raw_snippet/extraction_method
    # plus the two inputs confidence is derived from (source_tier,
    # claim_type), not a single pre-computed value -- see
    # storage/database.py's own field_provenance comment for why.
    # ═════════════════════════════════════════════════════

    def save_field_provenance(
        self, *, supplier_id: int, field_name: str, value: Optional[str],
        source_url: Optional[str], raw_snippet: Optional[str], extraction_method: str,
        source_tier: str, claim_type: str,
    ) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO field_provenance
                    (supplier_id, field_name, value, source_url, raw_snippet,
                     extraction_method, source_tier, claim_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (supplier_id, field_name, value, source_url, raw_snippet,
                 extraction_method, source_tier, claim_type),
            )
            return cur.lastrowid

    def get_field_provenance(
        self, supplier_id: int, field_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            if field_name:
                rows = conn.execute(
                    "SELECT * FROM field_provenance WHERE supplier_id = ? AND field_name = ? ORDER BY created_at",
                    (supplier_id, field_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM field_provenance WHERE supplier_id = ? ORDER BY field_name, created_at",
                    (supplier_id,),
                ).fetchall()
            return _rows_to_dicts(rows)

    # ═════════════════════════════════════════════════════
    # Supplier monitoring/diff (v30) -- supplier_snapshots is an
    # append-only observation log (no UNIQUE -- every check writes a
    # new row even when unchanged, same "written even when nothing
    # changed" discipline as verification_history) and
    # supplier_monitoring_settings is the opt-in registry. See
    # monitoring/monitoring_service.py.
    # ═════════════════════════════════════════════════════

    def save_snapshot(self, *, supplier_id: int, field_name: str, value: Optional[str]) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO supplier_snapshots (supplier_id, field_name, value) VALUES (?, ?, ?)",
                (supplier_id, field_name, value),
            )
            return cur.lastrowid

    def get_snapshots(
        self, supplier_id: int, field_name: Optional[str] = None, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Ordered `captured_at ASC, id ASC` (oldest first) -- same
        convention as get_field_provenance, so a caller comparing "the
        last two" reads list[-2:] rather than reversing anything."""
        with connection_scope(self.db_path) as conn:
            if field_name:
                rows = conn.execute(
                    "SELECT * FROM supplier_snapshots WHERE supplier_id = ? AND field_name = ? "
                    "ORDER BY captured_at ASC, id ASC LIMIT ?",
                    (supplier_id, field_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM supplier_snapshots WHERE supplier_id = ? "
                    "ORDER BY field_name, captured_at ASC, id ASC LIMIT ?",
                    (supplier_id, limit),
                ).fetchall()
            return _rows_to_dicts(rows)

    def get_monitoring_settings(self, supplier_id: int) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM supplier_monitoring_settings WHERE supplier_id = ?",
                (supplier_id,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_monitoring_settings(
        self, *, supplier_id: int, cadence: str, next_check_due_at: str,
        last_checked_at: Optional[str] = None,
    ) -> None:
        """Insert or update the one row for this supplier. `cadence`
        and `next_check_due_at` are always overwritten (an opt-in
        change replaces the prior schedule outright, matching
        upsert_audit_verdict's own single-current-value semantics --
        deliberately NOT append-only, unlike supplier_snapshots
        itself: there's only one "current" cadence for a supplier, not
        a history of them)."""
        with connection_scope(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO supplier_monitoring_settings
                    (supplier_id, cadence, next_check_due_at, last_checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (supplier_id) DO UPDATE SET
                    cadence = excluded.cadence,
                    next_check_due_at = excluded.next_check_due_at,
                    last_checked_at = COALESCE(excluded.last_checked_at, supplier_monitoring_settings.last_checked_at)
                """,
                (supplier_id, cadence, next_check_due_at, last_checked_at),
            )

    def delete_monitoring_settings(self, supplier_id: int) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute("DELETE FROM supplier_monitoring_settings WHERE supplier_id = ?", (supplier_id,))

    def get_suppliers_due_for_monitoring(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            query = (
                "SELECT * FROM supplier_monitoring_settings "
                "WHERE next_check_due_at <= CURRENT_TIMESTAMP ORDER BY next_check_due_at ASC"
            )
            if limit is not None:
                query += " LIMIT ?"
                rows = conn.execute(query, (limit,)).fetchall()
            else:
                rows = conn.execute(query).fetchall()
            return _rows_to_dicts(rows)

    # ═════════════════════════════════════════════════════
    # Buyer profiles (v10) -- named, reusable search + commercial
    # preference bundles. See pipeline.buyer_profile_search's own
    # docstring for exactly how required_capabilities/
    # destination_country (filtered) differ from preferred_incoterm/
    # min_company_size/target_market/min_export_experience_years
    # (scored, not filtered).
    # ═════════════════════════════════════════════════════

    def create_buyer_profile(self, *, name: str, **fields: Any) -> int:
        """`fields` may include any of: destination_country,
        required_capabilities (a list, JSON-encoded here),
        preferred_incoterm, preferred_payment_terms_days,
        min_company_size, target_market, min_export_experience_years,
        manufacturers_only. `name` must be unique -- raises
        `sqlite3.IntegrityError` on a duplicate, the same discipline
        every other unique-constrained insert in this codebase already
        has, rather than silently overwriting an existing profile.
        """
        payload = dict(fields)
        if "required_capabilities" in payload and payload["required_capabilities"] is not None:
            payload["required_capabilities"] = json.dumps(payload["required_capabilities"])

        columns = ["name", *payload.keys()]
        placeholders = ", ".join("?" for _ in columns)
        values = [name, *payload.values()]

        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                f"INSERT INTO buyer_profiles ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            return cur.lastrowid

    def get_buyer_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute("SELECT * FROM buyer_profiles WHERE id = ?", (profile_id,)).fetchone()
            return self._decode_buyer_profile(row)

    def get_buyer_profile_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute("SELECT * FROM buyer_profiles WHERE name = ?", (name,)).fetchone()
            return self._decode_buyer_profile(row)

    def list_buyer_profiles(self) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM buyer_profiles ORDER BY name").fetchall()
            return [self._decode_buyer_profile(row) for row in rows]

    def delete_buyer_profile(self, profile_id: int) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute("DELETE FROM buyer_profiles WHERE id = ?", (profile_id,))

    @staticmethod
    def _decode_buyer_profile(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        profile = dict(row)
        if profile.get("required_capabilities"):
            profile["required_capabilities"] = json.loads(profile["required_capabilities"])
        else:
            profile["required_capabilities"] = []
        return profile

    # ═════════════════════════════════════════════════════
    # Procurement outcomes (v10) -- feedback-loop schema and
    # repository methods only, per the commercial-intelligence spec's
    # own instruction. No UI, and nothing in this codebase currently
    # reads these back into scoring -- see
    # verification.commercial_probability's own note on this being the
    # future calibration source once real outcomes accumulate.
    # ═════════════════════════════════════════════════════

    def record_procurement_outcome(
        self, *, supplier_id: int, outcome: str,
        buyer_profile_id: Optional[int] = None, notes: Optional[str] = None,
    ) -> int:
        """`outcome` is free text by design (e.g. 'nda_signed',
        'rfq_submitted', 'quoted', 'accepted_ddp',
        'accepted_60_day_payment', 'tooling_order_won',
        'quality_approved', 'supplier_rejected') -- deliberately not
        constrained to a fixed list, the same lesson migrations v4 and
        v9 both already taught this codebase about CHECK constraints
        on an open-ended category column.
        """
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO procurement_outcomes (supplier_id, buyer_profile_id, outcome, notes) "
                "VALUES (?, ?, ?, ?)",
                (supplier_id, buyer_profile_id, outcome, notes),
            )
            return cur.lastrowid

    def get_procurement_outcomes(
        self, *, supplier_id: Optional[int] = None, buyer_profile_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if supplier_id is not None:
            clauses.append("supplier_id = ?")
            params.append(supplier_id)
        if buyer_profile_id is not None:
            clauses.append("buyer_profile_id = ?")
            params.append(buyer_profile_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM procurement_outcomes {where} ORDER BY recorded_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    # ═════════════════════════════════════════════════════
    # AI DISCOVERY / COLLECTION / VERIFICATION PLATFORM (v11)
    # See verification_ai/, collection/, discovery/ and this module's
    # own docstring pointer in CLAUDE.md for the overall architecture.
    # ═════════════════════════════════════════════════════

    def get_suppliers_needing_collection(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers with a known domain that Collection Service hasn't
        run against yet. Mirrors get_suppliers_needing_capability_extraction's
        never-attempted-vs-attempted timestamp-gated pattern exactly --
        `collection_status IS NULL` means "never run," not "ran and
        found nothing." This is also what makes augmenting the ~1,400
        existing suppliers (all already have a domain, none have ever
        had collection_status set) work with no special-casing.

        Ordered newest-first (id DESC) -- a real, observed confusion
        otherwise: with no ordering, SQLite returns whatever rows it
        happens to first (effectively id ASC in practice), so a bulk
        "collect/verify pending" run against a database that's mostly
        an old imported base processes the OLDEST suppliers first, not
        the ones a user just discovered and expected to see enriched.
        """
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE domain IS NOT NULL AND domain != '' ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE domain IS NOT NULL AND domain != ''
                    AND collection_status IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def get_suppliers_needing_contacts(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers with a known domain that ContactFinderService
        hasn't run against yet -- same never-attempted-vs-attempted
        `contacts_found_at IS NULL` pattern as
        get_suppliers_needing_collection, same newest-first ordering
        for the same reason (a bulk "find contacts" run should reach
        suppliers just discovered before the oldest rows in the
        database)."""
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE domain IS NOT NULL AND domain != '' ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE domain IS NOT NULL AND domain != ''
                    AND contacts_found_at IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def get_suppliers_needing_factory_facts(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers with a known domain that FactoryFactsService
        hasn't run against yet -- same never-attempted-vs-attempted
        `factory_facts_extracted_at IS NULL` pattern and newest-first
        ordering as get_suppliers_needing_contacts."""
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE domain IS NOT NULL AND domain != '' ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM suppliers
                    WHERE domain IS NOT NULL AND domain != ''
                    AND factory_facts_extracted_at IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def get_suppliers_needing_ai_verification(
        self, limit: int = 1000, force: bool = False
    ) -> List[Dict[str, Any]]:
        """Suppliers VerificationService.verify_pending() hasn't assessed
        yet -- gated on ai_confidence_assessed_at, same discipline as
        every other "needing_X" method in this file. Ordered newest-first
        (id DESC) -- see get_suppliers_needing_collection's own note on
        why unordered SQLite output means "the oldest suppliers get
        processed first" in practice, not what a user running a bulk
        pass right after a fresh discovery would expect."""
        with connection_scope(self.db_path) as conn:
            if force:
                rows = conn.execute("SELECT * FROM suppliers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM suppliers WHERE ai_confidence_assessed_at IS NULL ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def get_suppliers_needing_reverification(
        self, older_than_days: int, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Backs `main.py reverify --older-than-days` / a future
        scheduled reverify sweep -- suppliers never verified at all
        (`last_verified IS NULL`) OR verified more than `older_than_days`
        days ago. Ordered oldest-first so the most stale records get
        reverified before ones only marginally past the threshold."""
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM suppliers
                WHERE last_verified IS NULL
                   OR last_verified < datetime('now', ? || ' days')
                ORDER BY last_verified IS NOT NULL, last_verified ASC
                LIMIT ?
                """,
                (f"-{older_than_days}", limit),
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    def record_collection_run(
        self, *, supplier_id: int, status: str, pages_visited: int = 0,
        artifacts_dir: Optional[str] = None, proxy_provider: Optional[str] = None,
        error_message: Optional[str] = None, started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> int:
        """Writes one collection_runs row and updates the supplier's own
        collection_last_run_at/collection_status summary fields in the
        same call -- callers never need to remember to do both."""
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO collection_runs "
                "(supplier_id, status, pages_visited, artifacts_dir, proxy_provider, "
                "error_message, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (supplier_id, status, pages_visited, artifacts_dir, proxy_provider,
                 error_message, started_at, completed_at),
            )
            run_id = cur.lastrowid
        self.update_supplier_fields(supplier_id, {
            "collection_status": status,
            "collection_last_run_at": completed_at or datetime.now(timezone.utc).isoformat(),
        })
        return run_id

    def get_collection_runs(self, supplier_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM collection_runs WHERE supplier_id = ? "
                "ORDER BY completed_at DESC, id DESC LIMIT ?",
                (supplier_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_verification_history(
        self, *, supplier_id: int, verification_type: str, confidence_score: Optional[int] = None,
        verdict: Optional[str] = None, summary: Optional[str] = None,
        evidence: Optional[Any] = None, model_used: Optional[str] = None,
    ) -> int:
        """One row per verification RUN (not per field, unlike
        supplier_change_log) -- written even when nothing changed, since
        "we checked and confirmed everything's still the same" is
        itself a useful audit fact. `evidence` is JSON-encoded if not
        already a string."""
        evidence_json = evidence
        if evidence is not None and not isinstance(evidence, str):
            evidence_json = json.dumps(evidence, default=_json_default)
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO verification_history "
                "(supplier_id, verification_type, confidence_score, verdict, summary, "
                "evidence_json, model_used) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (supplier_id, verification_type, confidence_score, verdict, summary,
                 evidence_json, model_used),
            )
            return cur.lastrowid

    def get_verification_history(self, supplier_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM verification_history WHERE supplier_id = ? "
                "ORDER BY run_at DESC, id DESC LIMIT ?",
                (supplier_id, limit),
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                if d.get("evidence_json"):
                    try:
                        d["evidence_json"] = json.loads(d["evidence_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                results.append(d)
            return results

    def record_discovery_run(
        self, *, product_query: str, category: Optional[str] = None, country: Optional[str] = None,
        candidates_found: int = 0, candidates_validated: int = 0,
        candidates_rejected: int = 0, candidates_duplicate: int = 0,
    ) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO discovery_runs "
                "(product_query, category, country, candidates_found, candidates_validated, "
                "candidates_rejected, candidates_duplicate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (product_query, category, country, candidates_found, candidates_validated,
                 candidates_rejected, candidates_duplicate),
            )
            return cur.lastrowid

    def find_discovered_suppliers(
        self, product: str, discovery_source: str = "discovery_service",
    ) -> List[Dict[str, Any]]:
        """Every supplier discover() ever created for `product`, however
        many separate calls it took to find them (CLI or API, --source
        serpapi or llm) -- the bridge discovery.discovery_service.
        DiscoveryService.export_for_batch_upload() reads from.

        Deliberately NOT based on pipeline_jobs or discovery_runs:
        neither reliably tracks which supplier rows a given run created.
        discovery_runs (record_discovery_run above) only ever stores
        summary counts, never the actual created ids. pipeline_jobs only
        gets a row at all for an API-triggered run (POST /discovery/jobs
        -> api.jobs.run_discovery_job) -- a plain `main.py discover`
        CLI invocation writes neither. What IS reliable: discovery.
        discovery_service.DiscoveryService._process_candidate sets
        discovery_source='discovery_service' and product_keywords=
        [product] directly on the supplier row itself, unconditionally,
        regardless of caller -- persistent state, not run bookkeeping,
        so this stays correct across however many discover() calls it
        took to reach a target count.

        product_keywords is matched as the exact quoted JSON-array
        element (`%"{product}"%`), not a bare substring -- avoids
        cross-matching a different-but-textually-overlapping product
        term (the same discipline search_suppliers_full's own
        product_query LIKE already uses, just precise to the exact
        string _process_candidate wrote rather than a loose substring).

        Excludes flagged=1 suppliers -- same human-only "not a valid
        candidate" marker search_suppliers/search_suppliers_full
        exclude. Without this, a company someone has already ruled out
        for one product category (e.g. a broker/network, not a single
        factory) could silently resurface as an export-ready candidate
        the next time a DIFFERENT product search's keywords happen to
        also match its product_keywords -- exactly the scenario this
        exclusion exists to prevent.
        """
        pattern = f'%"{product}"%'
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM suppliers WHERE discovery_source = ? AND product_keywords LIKE ? "
                "AND flagged = 0 ORDER BY id",
                (discovery_source, pattern),
            ).fetchall()
            return _rows_to_dicts(rows, SUPPLIER_JSON_FIELDS)

    # ═════════════════════════════════════════════════════
    # Sourcing Agent runs (v12) -- one row per chat brief, scoping its
    # qualified results + CSV download to exactly that request. See
    # sourcing/sourcing_agent.py.
    # ═════════════════════════════════════════════════════

    def record_sourcing_run(
        self, *, brief_text: str, target_count: int, structured_brief: Optional[Dict[str, Any]] = None,
    ) -> int:
        with connection_scope(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO sourcing_runs (brief_text, structured_brief_json, target_count) "
                "VALUES (?, ?, ?)",
                (
                    brief_text,
                    json.dumps(structured_brief, default=_json_default) if structured_brief is not None else None,
                    target_count,
                ),
            )
            return cur.lastrowid

    def complete_sourcing_run(
        self, run_id: int, *, qualified_supplier_ids: List[int], examined_count: int,
        status: str = "completed", error_message: Optional[str] = None,
    ) -> None:
        with connection_scope(self.db_path) as conn:
            conn.execute(
                "UPDATE sourcing_runs SET status = ?, examined_count = ?, "
                "qualified_supplier_ids_json = ?, error_message = ?, completed_at = ? WHERE id = ?",
                (
                    status, examined_count, json.dumps(qualified_supplier_ids), error_message,
                    datetime.now(timezone.utc).isoformat(), run_id,
                ),
            )

    def get_sourcing_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            row = conn.execute("SELECT * FROM sourcing_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            run = dict(row)
            for field in ("structured_brief_json", "qualified_supplier_ids_json"):
                if run.get(field):
                    run[field] = json.loads(run[field])
            return run

    def list_sourcing_runs(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        with connection_scope(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM sourcing_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            runs = []
            for row in rows:
                run = dict(row)
                for field in ("structured_brief_json", "qualified_supplier_ids_json"):
                    if run.get(field):
                        run[field] = json.loads(run[field])
                runs.append(run)
            return runs
