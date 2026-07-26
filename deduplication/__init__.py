from deduplication.name_utils import normalise_company_name
from deduplication.domain_utils import extract_domain, domains_match, is_platform_subdomain
from deduplication.matcher import SupplierMatcher

__all__ = [
    "normalise_company_name",
    "extract_domain",
    "domains_match",
    "is_platform_subdomain",
    "SupplierMatcher",
]
