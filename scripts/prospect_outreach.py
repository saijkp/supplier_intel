"""Local CLI: imports never send; routine requires explicit environment enablement."""
import argparse
import csv
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from outreach.service import OutreachService
from outreach.repository import OutreachRepository
from storage.database import initialise_schema


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,help='Explicit SQLite path; never Railway/production')
    subs=parser.add_subparsers(dest='command',required=True)
    imp=subs.add_parser('import'); imp.add_argument('path'); imp.add_argument('--source',required=True); imp.add_argument('--source-url',default='')
    subs.add_parser('routine'); subs.add_parser('metrics')
    args=parser.parse_args()
    initialise_schema(args.db)
    s=OutreachService(OutreachRepository(args.db))
    if args.command=='import':
        path=Path(args.path)
        with path.open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.DictReader(f)) if path.suffix.lower()=='.csv' else json.load(f)
        result=s.import_rows(rows,args.source,args.source_url or path.name)
    elif args.command=='routine':
        result=s.run_pending()
    else:
        result=s.metrics()
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
