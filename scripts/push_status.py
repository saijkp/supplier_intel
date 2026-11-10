"""Upload Markdown after deployment; reads current version before optimistic PUT."""
import argparse
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from rfq_agent import BASE, token


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('path', type=Path)
    p.add_argument('--doc', default='live')
    p.add_argument('--by', default='Sai')
    args = p.parse_args()
    content = args.path.read_text(encoding='utf-8-sig')
    if len(content.encode('utf-8')) > 200 * 1024:
        p.error('Status document exceeds 200 KB')
    headers = {'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json'}
    url = BASE + '/status/' + quote(args.doc, safe='')
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as r:
            expected = json.load(r)['updated_at']
    except HTTPError as exc:
        if exc.code != 404:
            raise SystemExit(f'Could not read status: HTTP {exc.code}') from None
        expected = None
    data = json.dumps(dict(content_md=content, updated_by=args.by, expected_updated_at=expected)).encode()
    try:
        with urlopen(Request(url, headers=headers, data=data, method='PUT'), timeout=60) as r:
            result = json.load(r)
    except HTTPError as exc:
        raise SystemExit('Someone else edited the status; review it before retrying.' if exc.code == 409 else f'Upload failed: HTTP {exc.code}') from None
    print('Saved status ' + args.doc + ' at ' + result['updated_at'])

if __name__ == '__main__':
    main()
