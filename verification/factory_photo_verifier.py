"""
verification/factory_photo_verifier.py

Uses an OpenAI vision-capable model to sanity-check whether a supplier's
factory photos plausibly show a real production facility consistent
with their claimed manufacturing capability — a check none of this
platform's other signals can perform, since it looks at physical
evidence rather than filed paperwork or platform metadata.

This closes a real gap: `openai` has been a project dependency since
this module was added, but nothing in the pipeline actually called the
API until now. It's the fifth manufacturer-verification signal,
alongside the four in verification.manufacturer_verifier.

IMPORTANT — NOT YET TESTED AGAINST A LIVE API CALL, AND NOT YET WIRED
INTO THE SCRAPERS:
  1. This has only been exercised against a fake injected client in
     tests/test_manufacturer_verification.py — I don't have an OpenAI
     API key in this environment. Confirm DEFAULT_MODEL is still
     current (and still vision-capable) before relying on it.
  2. No scraper in this codebase downloads photos yet — AlibabaScraper
     captures whatever the Apify actor returns, which may include image
     URLs, but nothing currently fetches the actual image bytes. This
     module takes `image_bytes` directly so it's independently usable
     and testable; wiring "fetch the photo, then call this" into the
     scraping stage is the natural next increment.
  3. Before downloading and re-processing any platform's photos, check
     that platform's terms of service — this doesn't ask the model to
     reproduce or redistribute the images, only to describe what it
     sees, but you're still the one making the download.
  4. Calibrate before trusting verdicts in bulk: run this against a
     handful of factories you've personally audited (you have 1,000+
     audits of real-world ground truth) and see whether the verdicts
     line up before relying on it for suppliers you haven't seen.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# CONFIRM this is still current (and still vision-capable) before live use.
DEFAULT_MODEL = "gpt-4o"

VALID_VERDICTS = ("plausible_factory", "implausible", "uncertain")

ASSESSMENT_PROMPT = """You are assessing whether a photo, submitted by a supplier on a B2B \
sourcing platform, plausibly shows a real manufacturing facility capable of producing the \
claimed product category — or whether it looks like a generic office, warehouse, showroom, \
or stock photo that doesn't support a manufacturing claim.

Claimed product category: {product_category}
Claimed company name: {company_name}

Respond in exactly this format:
VERDICT: [plausible_factory | implausible | uncertain]
REASONING: [1-3 sentences explaining what you see and why]
"""


class FactoryPhotoVerifier:

    def __init__(self, client: Optional[Any] = None, model: str = DEFAULT_MODEL):
        self.model = model
        self._client = client  # injected for tests; built lazily otherwise

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # imported lazily: optional dep for tests
            self._client = OpenAI()  # reads OPENAI_API_KEY from the environment
        return self._client

    def assess_photo(
        self,
        image_bytes: bytes,
        media_type: str,
        product_category: str,
        company_name: str = "",
    ) -> Dict[str, Any]:
        """Returns {'verdict': ..., 'reasoning': ...}. Never raises for a
        bad/ambiguous image or a failed API call — returns 'uncertain'
        with an explanation instead, since this is meant to be one
        signal among several, not a hard gate that can take down a
        batch job on one malformed image."""
        try:
            data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": ASSESSMENT_PROMPT.format(
                                product_category=product_category, company_name=company_name,
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }],
            )
            text = response.choices[0].message.content or ""
            return self._parse_response(text)
        except Exception as e:
            logger.error("Factory photo assessment failed: %s", e)
            return {"verdict": "uncertain", "reasoning": f"Assessment failed: {e}"}

    def assess_photos(
        self,
        photos: List[Dict[str, Any]],
        product_category: str,
        company_name: str = "",
    ) -> Dict[str, Any]:
        """Assess multiple photos and roll them up into one verdict.
        `photos` is a list of {'image_bytes': bytes, 'media_type': str}.

        Rollup logic: 'plausible_factory' if at least one photo is
        plausible and none are implausible; 'implausible' if at least
        one is implausible and none are plausible; 'uncertain' if the
        photos disagree with each other, or if nothing conclusive came
        back at all."""
        if not photos:
            return {"verdict": "uncertain", "reasoning": "No photos available to assess", "photo_count": 0}

        results = [
            self.assess_photo(p["image_bytes"], p["media_type"], product_category, company_name)
            for p in photos
        ]
        verdicts = [r["verdict"] for r in results]

        has_plausible = "plausible_factory" in verdicts
        has_implausible = "implausible" in verdicts

        if has_plausible and not has_implausible:
            overall = "plausible_factory"
        elif has_implausible and not has_plausible:
            overall = "implausible"
        else:
            overall = "uncertain"  # either contradictory evidence, or nothing conclusive

        return {
            "verdict": overall,
            "reasoning": "; ".join(r["reasoning"] for r in results),
            "photo_count": len(photos),
        }

    @staticmethod
    def _parse_response(text: str) -> Dict[str, Any]:
        verdict = "uncertain"
        reasoning = text.strip()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("VERDICT:"):
                value = stripped.split(":", 1)[1].strip().lower()
                if value in VALID_VERDICTS:
                    verdict = value
            if stripped.upper().startswith("REASONING:"):
                reasoning = stripped.split(":", 1)[1].strip()
        return {"verdict": verdict, "reasoning": reasoning}
