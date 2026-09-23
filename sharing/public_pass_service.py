"""
sharing/public_pass_service.py

The "Verified" trade-show pass: an opt-in, per-supplier, unauthenticated
public profile page (GET /public/suppliers/{token}, served by
frontend/verify.html) plus a QR code pointing at it, meant to be
printed onto a physical card and left on a supplier's booth table at
an exhibition -- a visitor scans it and sees the platform's own
verified facts about that supplier, no login required.

Why a random token, never the integer id
-----------------------------------------
suppliers.id is sequential and already used throughout this API for
authenticated lookups. Using it here too would mean anyone who scans
one pass could enumerate the entire supplier database just by
incrementing a number in the URL. public_token is a random, URL-safe
value (secrets.token_urlsafe) that carries no ordering information and
is unique across the whole table (enforced by both the DB's partial
UNIQUE index and this service's own generation retry).

What's on the public page, and what's deliberately NOT
--------------------------------------------------------
This service hand-picks a narrow, marketing-safe subset of the
suppliers row for get_public_profile -- registry/certification facts,
manufacturer-verification signals, and capability findings, all
already sourced from public registries or the supplier's own website.
It deliberately never includes: audit_verdicts or their notes (a
buyer's own private due-diligence call on this supplier, per CLAUDE.md
standing rule 2 -- never anyone else's to see, let alone a random
scanner's), composite_score/ai_confidence_score/recommendation/
procurement_recommendation (an internal ranking number or a category
like "avoid"/"review" has no business on a badge the supplier
themselves keeps on their own table), key_contacts/sourcing_* fields
(Apollo-sourced named contacts and another buyer's own brief-specific
notes), or flagged/flag_reason/notes (internal operational fields).
Generating a pass at all is a deliberate, manual, per-supplier action
by the platform operator -- nothing is made public by default.

Same repo/service split as monitoring/monitoring_service.py: the
mechanical reads/writes live on SupplierRepository
(enable_public_pass/disable_public_pass/get_public_pass/
get_supplier_by_public_token/public_token_in_use); this class holds
the actual rules (token generation and uniqueness, what "revoked"
means, what's safe to publish).
"""

from __future__ import annotations

import io
import secrets
from typing import Any, Dict, List, Optional

from config.settings import PUBLIC_SITE_BASE_URL
from storage.repository import SupplierRepository

# secrets.token_urlsafe(16) -> ~128 bits of entropy, base64url-encoded
# (about 22 characters) -- long enough that public_token_in_use's retry
# loop below is defensive icing, not a real collision risk in practice.
_TOKEN_BYTES = 16
_MAX_GENERATION_ATTEMPTS = 5

# Capped so the public page (and the physical card it's printed from)
# stays short -- the strongest/most-recent findings first, since
# get_capabilities already orders by assessed_at DESC.
_MAX_PUBLIC_CAPABILITIES = 8


class PublicPassService:
    def __init__(self, repo: SupplierRepository):
        self.repo = repo

    def _generate_unique_token(self) -> str:
        for _ in range(_MAX_GENERATION_ATTEMPTS):
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            if not self.repo.public_token_in_use(token):
                return token
        raise RuntimeError(
            f"Could not generate a unique public_token after {_MAX_GENERATION_ATTEMPTS} attempts "
            "-- this should be virtually impossible at 128 bits of entropy; treat as a bug, not bad luck."
        )

    def create_pass(self, supplier_id: int, *, regenerate: bool = False) -> Dict[str, Any]:
        """Idempotent by default: calling this again for a supplier that
        already has an active pass just returns it unchanged. A
        previously-revoked pass is reinstated with the SAME token (so a
        physical card already printed and handed out works again,
        without a reprint) unless `regenerate=True`, which always issues
        a brand-new token -- the old one then 404s permanently, for the
        case where a printed card was lost or the operator wants a clean
        break rather than a reinstated one."""
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise ValueError(f"No supplier with id {supplier_id}")

        existing = self.repo.get_public_pass(supplier_id)
        already_active = existing is not None and not existing["public_pass_revoked_at"]
        if already_active and not regenerate:
            token = existing["public_token"]
        elif existing is not None and not regenerate:
            # Revoked, reinstating: keep the same physical QR working.
            token = existing["public_token"]
            self.repo.enable_public_pass(supplier_id, token=token)
        else:
            token = self._generate_unique_token()
            self.repo.enable_public_pass(supplier_id, token=token)

        return self.get_pass_status(supplier_id)  # type: ignore[return-value]

    def revoke_pass(self, supplier_id: int) -> None:
        if self.repo.get_supplier(supplier_id) is None:
            raise ValueError(f"No supplier with id {supplier_id}")
        self.repo.disable_public_pass(supplier_id)

    def get_pass_status(self, supplier_id: int) -> Optional[Dict[str, Any]]:
        """None if no pass has ever been generated for this supplier.
        Otherwise the pass's token/timestamps plus the public URL it
        resolves to, whether or not it's currently revoked -- callers
        that need to distinguish active from revoked check
        `public_pass_revoked_at` themselves."""
        status = self.repo.get_public_pass(supplier_id)
        if status is None:
            return None
        return {**status, "public_url": self.build_public_url(status["public_token"])}

    def build_public_url(self, token: str) -> str:
        return f"{PUBLIC_SITE_BASE_URL}/verify.html?t={token}"

    def get_public_profile(self, token: str) -> Optional[Dict[str, Any]]:
        """The curated, public-safe payload frontend/verify.html renders,
        or None if the token doesn't exist or has been revoked (see
        SupplierRepository.get_supplier_by_public_token -- both cases
        look identical from here, deliberately). See this module's own
        docstring for exactly what is and isn't included, and why."""
        supplier = self.repo.get_supplier_by_public_token(token)
        if supplier is None:
            return None

        capabilities = self._public_capabilities(supplier["id"])
        uk_registry = None
        if supplier.get("companies_house_match_status") == "verified":
            uk_registry = {
                "companies_house_number": supplier.get("companies_house_number"),
                "status": supplier.get("companies_house_status"),
                "incorporated_at": supplier.get("companies_house_incorporated_at"),
            }

        return {
            "canonical_name": supplier["canonical_name"],
            "country": supplier.get("country"),
            "city": supplier.get("city"),
            "domain": supplier.get("domain"),
            "year_established": supplier.get("year_established"),
            "is_manufacturer": supplier.get("is_manufacturer"),
            "manufacturer_signals": supplier.get("manufacturer_signals") or [],
            "certifications": {
                "iso_9001": bool(supplier.get("iso_9001")),
                "iso_9001_expiry": supplier.get("iso_9001_expiry"),
                "iso_ts_16949": bool(supplier.get("iso_ts_16949")),
                "iatf_16949": bool(supplier.get("iatf_16949")),
                "ce_certified": bool(supplier.get("ce_certified")),
                "ukca_certified": bool(supplier.get("ukca_certified")),
                "e_mark_certified": bool(supplier.get("e_mark_certified")),
                "other_certifications": supplier.get("other_certifications") or [],
            },
            "uk_registry": uk_registry,
            "primary_categories": supplier.get("primary_categories") or [],
            "capabilities": capabilities,
            "linkedin_url": supplier.get("linkedin_url"),
            "last_verified": supplier.get("last_verified"),
            "pass_active_since": supplier.get("public_pass_created_at"),
        }

    def _public_capabilities(self, supplier_id: int) -> List[Dict[str, Any]]:
        findings = self.repo.get_capabilities(supplier_id)[:_MAX_PUBLIC_CAPABILITIES]
        return [
            {
                "term": finding.get("canonical_term") or finding.get("reported_term"),
                "category": finding.get("category"),
                "relationship": finding.get("relationship"),
                "evidence": finding.get("evidence"),
            }
            for finding in findings
        ]

    def generate_qr_png(self, token: str) -> bytes:
        """PNG bytes for a QR code encoding this token's public URL.
        Generated on the fly (not stored) -- cheap, and means a
        regenerated/reinstated token never needs a stale cached image
        invalidating. Callers should check get_public_profile(token) is
        not None first, so a QR image is never handed out for a token
        that would 404 (see api/app.py's public_pass_qr_code route)."""
        import qrcode  # imported lazily: only the QR-serving route needs this dependency

        img = qrcode.make(self.build_public_url(token))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
