"""Explainable fit classification: evidence is always a literal text span."""
import re


def fit(description, application_type=''):
    text = description or ''
    category = re.search(r'\b(?:Conditions|Advertising)\b', application_type, re.I)
    if category:
        return 0, [{'rule': 'excluded', 'evidence': category[0]}], None
    excluded = re.search(r'\b(?:householder|signage|advertisement|tree\w*|telecom\w*|discharge of conditions|approval of details|non.material amendment)\b', application_type, re.I)
    excluded = excluded or re.search(r'^\s*(?:householder|signage|advertisement|tree\w*|works to trees|felling|pruning|telecom\w*|discharge of conditions|approval of details|non.material amendment)\b', text, re.I)
    if excluded:
        return 0, [{'rule': 'excluded', 'evidence': excluded[0]}], None
    matches = list(re.finditer(r'\b(\d+)\s*(?:no\.?\s*)?(?:new\s+)?(?:(?:residential\s+)?(?:dwellings|flats|apartments|houses|homes)|residential\s+units)\b', text, re.I))
    count = int(matches[0][1]) if len(matches) == 1 else None
    rules = [
        ('shared living', r'\b(?:HMO|houses? in multiple occupation|co[ -]?living)\b'),
        ('student accommodation', r'\bstudent\s+(?:accommodation|housing|residences|bedrooms)\b'),
        ('hospitality', r'\b(?:hotel|serviced apartments?)\b'),
        ('care / later living', r'\b(?:care home|nursing home|later living|retirement|extra care)\b'),
        ('office fit-out', r'\boffice\b.{0,100}\b(?:fit[ -]?out|refurbishment)\b|\b(?:fit[ -]?out|refurbishment)\b.{0,100}\boffice\b'),
        ('high-end house full fit-out', r'\b(?:luxury|high[ -]end)\b.{0,100}\b(?:house|dwelling)\b.{0,100}\b(?:full fit[ -]?out|complete interior refurbishment)\b'),
    ]
    reasons = []
    for match in matches:
        if int(match[1]) >= 5:
            reasons.append({'rule': 'residential 5+ units', 'evidence': match[0]})
    for rule, pattern in rules:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            reasons.append({'rule': rule, 'evidence': match[0]})
    return (80 if reasons else 0), reasons, count
