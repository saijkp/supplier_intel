"""
verification/product_matcher.py

Verifies a scraped product listing is actually the product you meant —
not just a keyword match. Addresses a specific reported failure:
searching for "trailer lights" returned LED bulbs when what was wanted
was the complete rear combination lamp (tail light) assembly. Keyword
search alone can't reliably tell a component from the assembly it goes
inside; this module uses vision to check directly.

Two ways to use it:

1. classify_product() — given ONE candidate photo plus a text
   description of what you actually want ("complete trailer rear
   combination lamp assembly with housing, not a bare bulb"), judges
   whether the photo matches that description.
2. compare_to_references() — given one or more REFERENCE photos (e.g.
   photos of the exact part you need, or of a listing you know is
   right) plus a candidate photo, judges whether the candidate shows
   the same type of product. Supports multiple reference photos
   specifically so you can show several angles/examples of what you
   want, not just one.

Architecturally this mirrors verification.factory_photo_verifier — same
injectable-client, never-raises-just-returns-'uncertain' design — but
answers a different question: that module asks "is this a real
factory", this one asks "is this the product I actually meant".

NOT YET TESTED AGAINST A LIVE API CALL — only against a fake injected
client (tests/test_product_matcher.py). No scraper in this codebase
downloads candidate product photos yet either (same gap noted for
factory photos) — this takes image bytes directly so it's usable the
moment you have them, from any source (a scraper you extend, or a
photo you have locally).
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o"

VALID_VERDICTS = ("match", "partial_match", "no_match", "uncertain")

CLASSIFY_PROMPT = """You are checking whether a product photo from a B2B sourcing listing \
actually matches what a buyer is looking for — not just a keyword match, but the same type \
and completeness of product.

What the buyer wants: {target_description}

Look at the photo and judge whether it shows that specific thing, a close variant, or \
something different (e.g. a sub-component of it, like a bare bulb when a complete lamp \
housing was wanted, or vice versa).

Respond in exactly this format:
VERDICT: [match | partial_match | no_match | uncertain]
REASONING: [1-3 sentences explaining what you see and why it does or doesn't match]
"""

COMPARE_PROMPT = """You are comparing a candidate product photo from a B2B sourcing listing \
against one or more reference photos of the product a buyer actually wants, to check they are \
the same TYPE and completeness of product — not just visually similar in a generic sense.

{product_context}

The reference photos come first, showing what's wanted. The final photo is the candidate \
listing being evaluated. Judge whether the candidate is the same type of product as the \
references (e.g. both are complete assemblies with housings) or different (e.g. the candidate \
is just a sub-component, or a visually similar but functionally different part).

Respond in exactly this format:
VERDICT: [match | partial_match | no_match | uncertain]
REASONING: [1-3 sentences explaining the comparison]
"""


class ProductMatcher:

    def __init__(self, client: Optional[Any] = None, model: str = DEFAULT_MODEL):
        self.model = model
        self._client = client  # injected for tests; built lazily otherwise

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # imported lazily: optional dep for tests
            self._client = OpenAI()  # reads OPENAI_API_KEY from the environment
        return self._client

    def classify_product(
        self,
        image_bytes: bytes,
        media_type: str,
        target_description: str,
    ) -> Dict[str, Any]:
        """Check one candidate photo against a text description of what
        you actually want. Never raises — returns 'uncertain' with an
        explanation on any failure, so a batch check can log and
        continue rather than crash on one bad image."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": CLASSIFY_PROMPT.format(target_description=target_description)},
                        {"type": "image_url", "image_url": {"url": self._data_url(image_bytes, media_type)}},
                    ],
                }],
            )
            text = response.choices[0].message.content or ""
            return self._parse_response(text)
        except Exception as e:
            logger.error("Product classification failed: %s", e)
            return {"verdict": "uncertain", "reasoning": f"Assessment failed: {e}"}

    def compare_to_references(
        self,
        reference_photos: List[Dict[str, Any]],
        candidate_photo: Dict[str, Any],
        product_context: str = "",
    ) -> Dict[str, Any]:
        """Compare a candidate photo against one or more reference
        photos. `reference_photos` and `candidate_photo` are each
        {'image_bytes': bytes, 'media_type': str}. Supports multiple
        reference photos so you can show several angles or examples of
        the exact product you need, rather than being limited to one."""
        if not reference_photos:
            return {"verdict": "uncertain", "reasoning": "No reference photos provided", "reference_count": 0}

        content: List[Dict[str, Any]] = [
            {"type": "text", "text": COMPARE_PROMPT.format(
                product_context=f"What's wanted: {product_context}" if product_context else ""
            )},
        ]
        for ref in reference_photos:
            content.append({
                "type": "image_url",
                "image_url": {"url": self._data_url(ref["image_bytes"], ref["media_type"])},
            })
        content.append({
            "type": "image_url",
            "image_url": {"url": self._data_url(candidate_photo["image_bytes"], candidate_photo["media_type"])},
        })

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": content}],
            )
            text = response.choices[0].message.content or ""
            result = self._parse_response(text)
        except Exception as e:
            logger.error("Product comparison failed: %s", e)
            result = {"verdict": "uncertain", "reasoning": f"Assessment failed: {e}"}

        result["reference_count"] = len(reference_photos)
        return result

    @staticmethod
    def _data_url(image_bytes: bytes, media_type: str) -> str:
        return f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"

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
