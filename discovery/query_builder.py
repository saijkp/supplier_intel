"""
discovery/query_builder.py

Builds SerpAPI query variants from a product/category/country search --
the entire "AI-assisted web research" grounding source for Discovery
Service (see discovery/discovery_service.py's module docstring for the
full anti-hallucination pipeline this feeds). Purely mechanical string
building -- no LLM call, no freeform "list me suppliers" prompt
anywhere in this module.
"""

from __future__ import annotations

from typing import List, Optional

_QUERY_TEMPLATES: tuple = (
    '"{product}" manufacturer',
    '"{product}" supplier',
    '"{product}" factory',
)


def build_queries(product: str, category: Optional[str] = None, country: Optional[str] = None) -> List[str]:
    """One query variant per template, each optionally qualified by
    country. `category` is accepted but not yet folded into the query
    text beyond `product` -- kept on the signature for the CLI/API
    surface (`main.py discover --category ...`) and for
    discovery_runs.category, which records it regardless of whether the
    query text itself uses it yet."""
    queries = []
    for template in _QUERY_TEMPLATES:
        query = template.format(product=product)
        if country:
            query = f"{query} {country}"
        queries.append(query)
    return queries
