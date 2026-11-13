"""Explainable fit classification: evidence is always a literal text span."""
import re
import os


def fit(description, application_type='', min_units=None):
    min_units = int(os.getenv('PLANNING_MIN_UNITS', '4')) if min_units is None else min_units
    if min_units < 1:
        raise ValueError('min_units must be positive')
    text = description or ''
    unsuitable = re.search(r'certificate of lawfulness|request for observations', application_type, re.I) or re.search(r'^\s*(?:removal of condition|replacement fire exit)|to also be used by the general public', text, re.I)
    if unsuitable:
        return 0, [{'rule': 'excluded no evidenced new package', 'evidence': unsuitable[0]}], None
    category = re.search(r'\b(?:Conditions|Advertising)\b', application_type, re.I)
    if category:
        return 0, [{'rule': 'excluded', 'evidence': category[0]}], None
    excluded = re.search(r'\b(?:householder|signage|advertisement|tree\w*|telecom\w*|discharge of (?:planning )?conditions?|approval of details|details (?:pursuant|submitted)|(?:EIA )?scoping opinion|request for scoping opinion|EIA scoping|non.material amendment)\b', application_type, re.I)
    excluded = excluded or re.search(r'^\s*(?:householder|signage|advertisement|tree\w*|works to trees|felling|pruning|telecom\w*|discharge of (?:planning )?conditions?|approval of details|details (?:pursuant|submitted)|(?:EIA )?scoping opinion|request for scoping opinion|EIA scoping|non.material amendment)\b', text, re.I)
    if excluded:
        return 0, [{'rule': 'excluded', 'evidence': excluded[0]}], None
    junk = re.search(r'\b(?:glazing|roof plant|roof terrace|diesel|generator)\b', text, re.I)
    scope = re.search(r'\b(?:kitchens?|joinery|wardrobes?|fit[ -]?out|conversion|change of use|erection|construction|new accommodation)\b', text, re.I)
    if junk and not scope:
        return 0, [{'rule': 'excluded ancillary works', 'evidence': junk[0]}], None
    matches = list(re.finditer(r'\b(\d+)\s*(?:(?:x|no\.?)\s*)?(?:(?:new|self[ -]contained|residential)\s+)*(?:\d+[ -]bed(?:room)?\s+)?(?:(?:residential\s+)?(?:dwellings|flats|apartments|houses|homes|studios)|residential\s+units)\b', text, re.I))
    self_contained = re.search(r'\b(\d+)\s+self[ -]contained\s+units\b', text, re.I)
    if self_contained:
        matches.append(self_contained)
    count = int(matches[0][1]) if len(matches) == 1 else None
    # Only sum an explicit additive list, never repeated totals or before/after counts.
    if len(matches) > 1 and all(re.fullmatch(r'\s*(?:,\s*(?:and\s*)?|and\s*|plus\s*)', text[a.end():b.start()], re.I) for a, b in zip(matches, matches[1:])):
        count = sum(int(m[1]) for m in matches)
    listing = re.search(r'\b\d+\s*(?:x|no\.?)\s*\d+[ -]bed(?:room)?(?:\s*(?:,|and)\s*\d+\s*(?:x|no\.?)\s*\d+[ -]bed(?:room)?)+\s*(?:flats|apartments|homes)', text, re.I)
    if listing:
        count = sum(int(n) for n in re.findall(r'(\d+)\s*(?:x|no\.?)', listing[0], re.I))
    rules = [
        ('shared living', r'\b(?:HMO|houses? in multiple occupation|co[ -]?living)\b'),
        ('student accommodation', r'\bstudent\s+(?:accommodation|housing|residences|bedrooms)\b'),
        ('hospitality', r'\b(?:hotel|serviced apartments?)\b'),
        ('care / later living', r'\b(?:care home|nursing home|later living|retirement|extra care)\b'),
        ('office fit-out', r'\boffice\b.{0,100}\b(?:fit[ -]?out|refurbishment)\b|\b(?:fit[ -]?out|refurbishment)\b.{0,100}\boffice\b'),
        ('high-end house full fit-out', r'\b(?:luxury|high[ -]end)\b.{0,100}\b(?:house|dwelling)\b.{0,100}\b(?:full fit[ -]?out|complete interior refurbishment)\b'),
    ]
    reasons = []
    joinery = re.search(r'\b(?:conversion|fit[ -]?out|refurbishment)\b.{0,160}\b(?:kitchens?|joinery|wardrobes?)\b|\b(?:kitchens?|joinery|wardrobes?)\b.{0,160}\b(?:conversion|fit[ -]?out|refurbishment)\b', text, re.I)
    if joinery:
        reasons.append({'rule': 'interior kitchen / joinery scope', 'evidence': joinery[0]})
    if count is not None and count >= min_units:
        for match in ([listing] if listing else matches):
            reasons.append({'rule': f'residential {min_units}+ units', 'evidence': match[0]})
    for rule, pattern in rules:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            reasons.append({'rule': rule, 'evidence': match[0]})
    return (80 if reasons else 0), reasons, count
