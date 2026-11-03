"""Run a local RFQ preview with fictional offers and a separate SQLite file.

Usage: python scripts/run_rfq_preview.py
Open http://127.0.0.1:8765/workspace/rfq.html and use token rfq-local-preview.
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Always isolate the preview, including when the parent shell has API/DB settings.
os.environ['SUPPLIER_INTEL_DB_PATH'] = str(ROOT / 'data' / 'rfq_dashboard_preview.db')
os.environ['API_ACCESS_TOKEN'] = 'rfq-local-preview'

from storage.database import initialise_schema
from storage.quote_workspace_repository import QuoteWorkspaceRepository


def seed():
    initialise_schema()
    repo = QuoteWorkspaceRepository()
    if repo.list_rfqs():
        return
    rfq = repo.create_rfq({'title':'DEMO · Custom LED lighting', 'customer':'Fictional sample data',
                           'quantity':'100', 'unit':'pcs', 'mandatory_specs':{'material':'aluminium', 'wattage':'20W'}})
    for supplier, currency, price, material, missing in [
        ('Demo Factory A','USD',18,'aluminium',[]),
        ('Demo Factory B','CNY',145,'aluminium',['moq']),
        ('Demo Factory C','USD',16.5,'steel',[]),
        ('Demo Factory D','USD',21,'aluminium',['incoterm','named_place']),
    ]:
        fields = {'unit_price':price,'currency':currency,'quoted_quantity':100,'moq':50,
                  'incoterm':'FOB','named_place':'Shenzhen','production_lead_days':25,
                  'payment_terms':'30% deposit, 70% before shipment','valid_until':'2099-12-31'}
        fields = {k:v for k,v in fields.items() if k not in missing}
        specs = {'material':material,'wattage':'20W'}
        evidence = {**{k:f'{k}: {v}' for k,v in fields.items()},
                    **{'spec:'+k:f'{k}: {v}' for k,v in specs.items()}}
        repo.add_quote(rfq['id'], {'supplier_name':supplier, 'fields':fields,'quoted_specs':specs,
                                   'evidence':evidence,'source_text':'DEMO ONLY\n'+'\n'.join(evidence.values()),'deviations':[]})


if __name__ == '__main__':
    import uvicorn
    seed()
    uvicorn.run('api.app:app', host='127.0.0.1', port=8765)
