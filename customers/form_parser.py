"""Pure, conservative extraction of labelled sourcing-brief notification text."""
import json
import re

# Include unused labels as boundaries so their values never leak into another field.
LABELS = {
    'Lead Source': 'lead_source', 'Full name': 'name', 'Company (optional)': 'company',
    'Email': 'email', 'Phone / WhatsApp': 'phone', 'Property developer': None,
    'Project name': 'project_name', 'Location': 'location', 'Number of units': None,
    'Finish level': 'finish_level', 'New build': None, 'Cat': 'categories',
    'Design Support': 'design_support', 'Materials / finishes': 'materials',
    'Specific dimensions': 'dimensions', 'Required on-site date': 'required_date',
    'Approximate budget': 'budget', 'Reference images / links': 'references',
    'Anything else': 'anything_else',
}
_parts = [re.escape(x) + r"\s*\*?\s*:" for x in LABELS]
_parts.append(r"What do you need\?[^:\n]*:")
FIELD_RE = re.compile(r"(?<!\w)(" + '|'.join(_parts) + ')', re.I)
EMAIL_RE = re.compile(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+")


def parse_form(text):
    """Return only explicit label values; missing or malformed input stays blank."""
    result = {k: '' for k in LABELS.values() if k}
    result.update(requirement='', categories=[])
    if not isinstance(text, str):
        return result
    matches = list(FIELD_RE.finditer(text))
    for i, match in enumerate(matches):
        label = match.group().rstrip(':').strip().rstrip('*').strip()
        key = 'requirement' if label.lower().startswith('what do you need?') else next(
            (v for k, v in LABELS.items() if k.lower() == label.lower()), None)
        if not key or result.get(key):
            continue
        value = text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
        if key == 'email':
            value = value if EMAIL_RE.fullmatch(value) else ''
        if key == 'categories':
            try:
                value = json.loads(value)
                value = value if isinstance(value, list) and all(isinstance(x, str) for x in value) else []
            except (ValueError, TypeError, RecursionError):
                value = []
        result[key] = value
    return result


def form_qualification(fields):
    scope = '\n'.join(x for x in [', '.join(fields.get('categories') or []), fields.get('requirement', '')] if x)
    return {k: v for k, v in dict(scope=scope, location=fields.get('location', ''),
                                budget=fields.get('budget', ''), timeline=fields.get('required_date', '')).items() if v}


# Exact input names verified against https://ikeng.co.uk/.
NETLIFY_FIELDS = {
    'name': 'name', 'email': 'email', 'phone': 'phone', 'company': 'company',
    'location': 'location', 'cat': 'categories', 'finish_level': 'finish_level',
    'description': 'requirement', 'design_support': 'design_support',
    'delivery_date': 'required_date', 'budget': 'budget', 'references': 'references',
    'materials': 'materials', 'dimensions': 'dimensions', 'notes': 'anything_else',
    'lead_source': 'lead_source', 'project_name': 'project_name',
}


def netlify_data(payload):
    """Raw names take precedence, even when blank; human_fields is a fallback."""
    data = {}
    human = payload.get('human_fields')
    if isinstance(human, dict):
        labels = {label.lower(): key for label, key in LABELS.items() if key}
        inverse = {v: k for k, v in NETLIFY_FIELDS.items()}
        for label, value in human.items():
            clean = label.rstrip(':').strip().rstrip('*').strip().lower()
            target = 'requirement' if clean.startswith('what do you need?') else labels.get(clean)
            source = inverse.get(target) or (clean if clean in NETLIFY_FIELDS else None)
            if clean in {'bot-field', 'leave this empty'}:
                source = 'bot-field'
            if source:
                data[source] = value
    ordered = payload.get('ordered_human_fields')
    if isinstance(ordered, list):
        for field in ordered:
            if isinstance(field, dict) and isinstance(field.get('name'), str):
                data[field['name']] = field.get('value')
    if isinstance(payload.get('data'), dict):
        data.update(payload['data'])
    return data


def fix_mojibake(value):
    """Netlify's outgoing webhook body double-encodes non-ASCII (seen 8 Nov 2026: the
    stored submission had '£', the webhook sent 'Â£'). Undo it only when the text
    round-trips cleanly as UTF-8 read as Latin-1; anything else is left unchanged."""
    if not isinstance(value, str) or not any(c in value for c in 'ÂÃâ'):
        return value
    try:
        return value.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def parse_netlify_form(payload):
    fields = parse_form('')
    data = netlify_data(payload)
    for source, target in NETLIFY_FIELDS.items():
        value = data.get(source)
        if target == 'categories':
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (ValueError, RecursionError):
                    value = [value] if value.strip() else []
            fields[target] = [fix_mojibake(x).strip() for x in value if x.strip()] if (
                isinstance(value, list) and all(isinstance(x, str) for x in value)) else []
        elif isinstance(value, str):
            fields[target] = fix_mojibake(value).strip()
    if not EMAIL_RE.fullmatch(fields['email']):
        fields['email'] = ''
    return fields


def form_fingerprint(fields):
    """Transport-independent comparison over only the shared, explicit fields."""
    import hashlib
    normalized = parse_form('')
    normalized.update({k: fields[k] for k in normalized if k in fields})
    normalized['email'] = normalized['email'].lower()
    normalized['categories'] = sorted(normalized['categories'])
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
