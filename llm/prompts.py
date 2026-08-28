"""
llm/prompts.py

Shared LLM system prompts used by more than one call site. Lives under
llm/ (not discovery/ or scrapers/) specifically to avoid a circular
import: discovery/candidate_extractor.py already imports from
scrapers/company_website_finder.py, so company_website_finder.py
cannot import anything from discovery/candidate_validator.py (or
anything that imports it) without creating a cycle. A neutral,
dependency-free home lets both sides import the same prompt text
instead of maintaining two copies that could silently drift apart.
"""

from __future__ import annotations

GROUNDED_COMPANY_NAME_EXTRACTION_SYSTEM_PROMPT = """You are reading the text of a company website. Extract ONLY what is explicitly stated in the text below -- never guess, infer, or fill in based on typical industry patterns or the domain name.

Rules, strictly enforced:
1. Only report a company name if it is explicitly stated in the text (e.g. in a heading, footer, "About Us" section, or copyright notice).
2. If the company name is not clearly stated, return null for company_name -- do not guess it from the domain or from context.
3. Only report a country if it is explicitly stated (an address, a phone country code mentioned as text, "based in X").
4. Never invent certifications, products, or history not present in the text -- this task only asks for name and country.

Return ONLY a JSON object with exactly these keys, no other text:
{
  "company_name": "the exact company name as stated in the text, or null if not clearly stated",
  "country": "the country as stated in the text, or null if not clearly stated"
}"""
