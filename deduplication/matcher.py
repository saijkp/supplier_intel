"""
deduplication/matcher.py

Golden-record deduplication engine. Given a normalizer's output dict for
a newly-scraped supplier ("candidate"), find_match() decides whether it
is the same company as an existing golden record — and resolve_and_store()
takes the appropriate repository action once that decision is made.

Match hierarchy (highest confidence first), thresholds from config.settings:
1. USCC exact match      -> 1.00 (certain — China's unified registry ID)
2. Domain exact match    -> 0.95 (near certain — same company website)
3. Name + country fuzzy  -> up to ~1.00, boosted by phone/city signals,
                            only reported at all above DEDUP_REJECT_THRESHOLD
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

from rapidfuzz import fuzz

from config.settings import (
    DEDUP_AUTO_MERGE_THRESHOLD,
    DEDUP_HUMAN_REVIEW_THRESHOLD,
    DEDUP_REJECT_THRESHOLD,
)
from deduplication.domain_utils import extract_domain
from deduplication.name_utils import normalise_company_name
from storage.repository import SupplierRepository

logger = logging.getLogger(__name__)


class SupplierMatcher:

    def __init__(self, repo: SupplierRepository):
        self.repo = repo

    # ═════════════════════════════════════════════════════
    # Matching
    # ═════════════════════════════════════════════════════

    def find_match(self, candidate: Dict[str, Any]) -> Tuple[Optional[int], float, Dict[str, Any]]:
        """
        Returns (supplier_id, confidence, match_signals).
        supplier_id = None means no usable match was found -> caller
        should treat this candidate as a brand-new supplier.
        """
        # ── Level 1: USCC exact match ──
        uscc = (candidate.get("uscc") or "").strip()
        if uscc and len(uscc) == 18:
            match = self.repo.find_by_uscc(uscc)
            if match:
                return match["id"], 1.0, {"uscc_match": True}

        # ── Level 2: Domain exact match ──
        domain = extract_domain(
            candidate.get("domain")
            or candidate.get("website")
            or candidate.get("alibaba_url")
            or ""
        )
        if domain:
            match = self.repo.find_by_domain(domain)
            if match:
                return match["id"], 0.95, {"domain_match": True}

        # ── Level 3: Fuzzy name matching within country ──
        name = normalise_company_name(candidate.get("canonical_name", ""))
        if not name:
            return None, 0.0, {}

        country = candidate.get("country") or ""
        candidate_pool = self.repo.find_by_country(country)

        best_score = 0.0
        best_match: Optional[Dict[str, Any]] = None
        best_signals: Dict[str, Any] = {}

        for existing in candidate_pool:
            existing_name = normalise_company_name(existing.get("canonical_name", ""))
            if not existing_name:
                continue

            name_score = fuzz.ratio(name, existing_name) / 100.0
            local_signals: Dict[str, Any] = {}

            if self._phones_match(candidate, existing):
                name_score = min(1.0, name_score + 0.15)
                local_signals["phone_match"] = True
            if self._cities_match(candidate, existing):
                name_score = min(1.0, name_score + 0.10)
                local_signals["city_match"] = True

            if name_score > best_score:
                best_score = name_score
                best_match = existing
                best_signals = local_signals

        if best_match and best_score >= DEDUP_REJECT_THRESHOLD:
            best_signals["name_score"] = round(best_score, 3)
            return best_match["id"], best_score, best_signals

        return None, 0.0, {}

    # ═════════════════════════════════════════════════════
    # Resolution — decide + act
    # ═════════════════════════════════════════════════════

    def resolve_and_store(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run find_match() and take the corresponding repository action:

        - confidence >= DEDUP_AUTO_MERGE_THRESHOLD:   merge into the matched golden record
        - confidence >= DEDUP_HUMAN_REVIEW_THRESHOLD: create a new golden record for the
                                                       candidate AND queue it against the
                                                       matched record for human review
                                                       (dedup_candidates requires two real
                                                       supplier ids, so nothing is left
                                                       un-persisted while awaiting review)
        - otherwise:                                  create a new golden record

        Returns a dict describing what happened, for pipeline stats/logging.
        """
        if not candidate.get("canonical_name"):
            raise ValueError("candidate must include a non-empty 'canonical_name'")

        supplier_id, confidence, signals = self.find_match(candidate)

        if supplier_id is not None and confidence >= DEDUP_AUTO_MERGE_THRESHOLD:
            self.repo.merge_into_golden(supplier_id, candidate)
            logger.info(
                "Auto-merged '%s' into golden record #%s (confidence=%.3f)",
                candidate["canonical_name"], supplier_id, confidence,
            )
            return {
                "action": "merged",
                "supplier_id": supplier_id,
                "confidence": confidence,
                "signals": signals,
            }

        if supplier_id is not None and confidence >= DEDUP_HUMAN_REVIEW_THRESHOLD:
            new_id = self.repo.create_golden_record(candidate)
            dedup_candidate_id = self.repo.add_to_review_queue(
                supplier_id_a=new_id,
                supplier_id_b=supplier_id,
                match_score=confidence,
                match_signals=signals,
            )
            logger.info(
                "Queued possible duplicate: new #%s vs existing #%s (confidence=%.3f)",
                new_id, supplier_id, confidence,
            )
            return {
                "action": "review_queued",
                "new_supplier_id": new_id,
                "matched_supplier_id": supplier_id,
                "confidence": confidence,
                "signals": signals,
                "dedup_candidate_id": dedup_candidate_id,
            }

        new_id = self.repo.create_golden_record(candidate)
        logger.info("Created new golden record #%s: %s", new_id, candidate["canonical_name"])
        return {
            "action": "created",
            "supplier_id": new_id,
            "confidence": confidence,
            "signals": signals,
        }

    # ═════════════════════════════════════════════════════
    # Signal helpers
    # ═════════════════════════════════════════════════════

    @staticmethod
    def _phones_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        """Compare the last 10 digits of each phone number, ignoring
        formatting/country-code prefixes. A deliberately loose but
        cheap heuristic — full E.164 parsing is unnecessary here since
        this is only ever a confidence *booster*, never the sole signal."""
        phone_a = re.sub(r"\D", "", str(a.get("primary_phone") or ""))[-10:]
        phone_b = re.sub(r"\D", "", str(b.get("primary_phone") or ""))[-10:]
        return bool(phone_a) and phone_a == phone_b

    @staticmethod
    def _cities_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        city_a = str(a.get("city") or "").strip().lower()
        city_b = str(b.get("city") or "").strip().lower()
        return bool(city_a) and city_a == city_b
