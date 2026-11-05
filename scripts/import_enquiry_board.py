"""Import the Codex enquiry board into Supplier Intel's customer tracker.

    python scripts/import_enquiry_board.py                 # dry run: prints what would be created
    python scripts/import_enquiry_board.py --limit 3 --apply
    python scripts/import_enquiry_board.py --apply

Reads enquiry-action-board.json (the working board) and, for IKE- references, the
Desktop folder register. Idempotent: each enquiry gets source_ref "board:<name>", and
existing ones are skipped. Grounded only: a phone/WhatsApp number is taken from the
board label or its verified_whatsapp_number field. Email addresses merely *mentioned*
in a record are NOT applied to the profile (they are often supplier addresses); they
are listed in the customer's notes for a human to confirm.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from si import call  # noqa: E402

BOARD = r"C:\Users\HP\Documents\Codex\2026-10-13\i\outputs\enquiry-action-board.json"
REGISTER = r"C:\Users\HP\Desktop\IK ENG Customer Enquiries\FOLDER REGISTER.json"
PHONE_RE = re.compile(r'\+\d[\d ]{7,16}\d')
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
MAX = 19000


def text(value):
    if value is None:
        return None
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return value[:MAX] or None


def date(value):
    value = (value or '')[:10] if isinstance(value, str) else ''
    return value if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None


def stamp(value):
    if not isinstance(value, str) or not re.match(r'\d{4}-\d{2}-\d{2}', value):
        return None
    return value.replace('T', ' ')[:19]


def build(record, refs):
    label = record['enquiry'].strip()
    parts = [p.strip() for p in label.split(' / ')]
    phone_match = PHONE_RE.search(label)
    phone = phone_match.group(0).replace(' ', '') if phone_match else None
    verified = record.get('verified_whatsapp_number')
    if not phone and isinstance(verified, str) and PHONE_RE.search(verified):
        phone = PHONE_RE.search(verified).group(0).replace(' ', '')
    name_parts = [p for p in parts if not PHONE_RE.fullmatch(p)]
    name = name_parts[0] if name_parts else label
    title = ' / '.join(name_parts[1:]) or 'enquiry'
    mentioned = sorted(set(EMAIL_RE.findall(json.dumps(record, ensure_ascii=False))) - {'sai@ikeng.online'})
    notes = 'Imported from the Codex enquiry board.'
    if mentioned:
        notes += ' Email addresses mentioned in the board record (confirm which, if any, is the customer): ' \
                 + ', '.join(mentioned[:15])
    customer = {'name': name, 'source': 'Codex enquiry board', 'notes': notes}
    if phone:
        customer['phone'] = phone
        customer['whatsapp'] = phone
    enquiry = {
        'title': title, 'status': (record.get('status') or 'new').strip(),
        'owner': text(record.get('owner')), 'next_action': text(record.get('next_action')),
        'next_action_due': date(record.get('review_due')), 'blocker': text(record.get('blocker')),
        'summary': text(record.get('current_summary')), 'requirements': text(record.get('current_requirements')),
        'last_customer_contact_at': stamp(record.get('customer_last_incoming_time') or record.get('last_incoming_time')),
        'last_our_contact_at': stamp(record.get('customer_last_outgoing_time') or record.get('last_outgoing_time')),
        'source_ref': 'board:' + label,
    }
    fee = record.get('sourcing_fee')
    if isinstance(fee, dict) and fee.get('amount_GBP'):
        enquiry['estimated_value'] = f"Sourcing fee GBP {fee['amount_GBP']}"
    ref = refs.get(label)
    if ref:
        enquiry['reference'] = ref
    return customer, {k: v for k, v in enquiry.items() if v is not None}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--board', default=BOARD)
    args = ap.parse_args()
    board = json.load(open(args.board, encoding='utf-8'))
    refs = {}
    if os.path.exists(REGISTER):
        for r in json.load(open(REGISTER, encoding='utf-8')).get('records', []):
            if r.get('ref') and r['ref'] not in refs.values():
                refs[r['enquiry'].strip()] = r['ref']
    records = board['enquiries'][:args.limit] if args.limit else board['enquiries']
    existing = set()
    if args.apply:
        existing = {e.get('source_ref') for e in call('GET', '/customers/enquiries?include_closed=true')}
    created = skipped = 0
    for record in records:
        customer, enquiry = build(record, refs)
        print(f"- {customer['name']:<28} {enquiry['status']:<20} due={enquiry.get('next_action_due') or '-':<10} "
              f"ref={enquiry.get('reference', 'new')} phone={'yes' if customer.get('phone') else 'no'}")
        if not args.apply:
            continue
        if enquiry['source_ref'] in existing:
            skipped += 1
            continue
        c = call('POST', '/customers', customer)
        call('POST', f"/customers/{c['id']}/enquiries", enquiry)
        created += 1
    print(f"\n{len(records)} board enquiries; " + (f"created {created}, skipped {skipped} already imported"
                                                     if args.apply else 'dry run, nothing written (use --apply)'))


if __name__ == '__main__':
    main()
