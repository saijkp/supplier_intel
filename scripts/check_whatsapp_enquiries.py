"""Daily safety net: WhatsApp senders who are not yet in the Customers tab.

WhatsApp on the business phone has no feed into Supplier Intel, so new enquiries only
arrive when someone adds them. This script looks everywhere WhatsApp chats show up on
this PC and lists every sender that doesn't match a customer's phone/WhatsApp or name:

  - WhatsApp chat exports (.txt, or .zip containing chat.txt) in Downloads, Desktop, Documents
  - Codex session logs (~/.codex/sessions), which hold every chat export Codex has read

    python scripts/check_whatsapp_enquiries.py              # last 3 days
    python scripts/check_whatsapp_enquiries.py --days 14

Read-only: it never messages anyone and never changes the database. The report is printed
and written to "IK ENG Customer Enquiries/NOT YET IN CUSTOMERS.txt" on the Desktop.
Numbers/names that are not customers (suppliers, friends, partners) can be listed one per
line in "IK ENG Customer Enquiries/WHATSAPP NOT CUSTOMERS.txt" to stop them being reported.
Limitation: chats Codex only *looked at* in WhatsApp Web (not exported) may not be in a
format this can read, so the AGENTS.md rule (create the customer before replying) still applies.
"""
import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from si import call  # noqa: E402

HOME = Path.home()
FOLDER = HOME / 'Desktop' / 'IK ENG Customer Enquiries'
REPORT = FOLDER / 'NOT YET IN CUSTOMERS.txt'
IGNORE = FOLDER / 'WHATSAPP NOT CUSTOMERS.txt'
LINE = re.compile(r'\[(\d{1,2}/\d{1,2}/\d{2,4}), (\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\] '
                  r'([^:\]\\\n]{2,40}): ((?:(?!\\n|\n).){0,200})')
LINE_ANDROID = re.compile(r'(?m)^(\d{1,2}/\d{1,2}/\d{2,4}), (\d{1,2}:\d{2}(?:\s?[ap]m)?) - ([^:\n]{2,40}): (.{0,200})')
SKIP_SENDERS = {'you', 'name', 'buyer', 'sender'}


def digits(value):
    d = re.sub(r'\D', '', value or '')
    return d[-10:] if len(d) >= 9 else ''


def texts(days):
    cutoff = time.time() - days * 86400
    for base in ('Downloads', 'Desktop', 'Documents'):
        root = HOME / base
        if not root.exists():
            continue
        for path in root.glob('*'):
            try:
                if path.stat().st_mtime < cutoff or 'whatsapp' not in path.name.lower():
                    continue
                if path.suffix.lower() == '.txt':
                    yield str(path), path.read_text(encoding='utf-8', errors='replace')
                elif path.suffix.lower() == '.zip':
                    with zipfile.ZipFile(path) as z:
                        for n in z.namelist():
                            if n.lower().endswith('.txt'):
                                yield f'{path}!{n}', io.TextIOWrapper(z.open(n), encoding='utf-8', errors='replace').read()
            except (OSError, zipfile.BadZipFile):
                continue
    sessions = HOME / '.codex' / 'sessions'
    if sessions.exists():
        for path in sessions.rglob('*.jsonl'):
            try:
                if path.stat().st_mtime >= cutoff:
                    yield f'Codex session {path.name}', path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=3)
    ap.add_argument('--poll', action='store_true', help='also check the mailbox (free): logs customer replies and '
                                                       'lists possible new email enquiries in the Customers tab')
    args = ap.parse_args()
    poll_line = ''
    if args.poll:
        try:
            r = call('POST', '/customers/poll', {})
            leads = call('GET', '/customers/leads')
            poll_line = (f"Email: {r['logged']} customer replies logged; {len(leads)} possible new email enquiries "
                         "waiting in Supplier Intel > Customers.")
        except SystemExit as exc:
            poll_line = f'Email check failed: {exc}'

    customers = call('GET', '/customers')
    known_digits = {digits(c.get(f)) for c in customers for f in ('phone', 'whatsapp')} - {''}
    known_names = [c['name'].strip().lower() for c in customers if c.get('name')]
    ignore = set()
    if IGNORE.exists():
        for line in IGNORE.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.split('#')[0].strip()
            if line:
                ignore.add(digits(line) or line.lower())

    found = {}
    for source, text in texts(args.days):
        for pattern in (LINE, LINE_ANDROID):
            for m in pattern.finditer(text):
                sender = m.group(3).strip()
                if sender.lower() in SKIP_SENDERS or sender.startswith('-'):
                    continue
                key = digits(sender) or sender.lower()
                if key in ignore:
                    continue
                if digits(sender):
                    if key in known_digits:
                        continue
                elif any(sender.lower() == n or sender.lower() in n or n in sender.lower() for n in known_names):
                    continue
                entry = found.setdefault(key, {'sender': sender, 'first': m.group(4).strip()[:160],
                                               'when': f'{m.group(1)} {m.group(2)}', 'sources': set(), 'messages': 0})
                entry['messages'] += 1
                entry['sources'].add(source)

    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [f'WhatsApp senders not yet in the Customers tab (checked {stamp}, last {args.days} days)', '']
    if poll_line:
        lines = [poll_line, ''] + lines
    if not found:
        lines.append('None. Every WhatsApp sender found matches a customer, or is on the not-customers list.')
    for e in sorted(found.values(), key=lambda x: -x['messages']):
        hint = '  (+86 number: probably a supplier)' if re.sub(r'\D', '', e['sender']).startswith('86') else ''
        lines += [f"* {e['sender']}{hint}", f"  first seen {e['when']}: \"{e['first']}\"",
                  f"  {e['messages']} message(s) in: {', '.join(sorted(e['sources']))[:300]}", '']
    lines += ['', 'For each real enquiry: add it in Supplier Intel > Customers (+ New customer & enquiry).',
              f'Not a customer? Add the number or name to {IGNORE.name} so it stops appearing.']
    report = '\n'.join(lines)
    print(report)
    try:
        FOLDER.mkdir(exist_ok=True)
        REPORT.write_text(report, encoding='utf-8')
    except OSError:
        pass
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
