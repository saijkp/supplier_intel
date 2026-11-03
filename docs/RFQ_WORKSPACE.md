# RFQ workspace

The RFQ workspace is linked from the existing supplier app, dashboard and console.
It uses the existing shared API token (one trusted internal workspace, no per-user
accounts). All four `/rfqs` endpoints require that token and refuse access if it
is missing on the server. RFQ and quote content is never exposed by public routes.

## Try the complete workflow locally

Install `requirements.txt`, then run:

```sh
python scripts/run_rfq_preview.py
```

Open <http://127.0.0.1:8765/workspace/rfq.html>. Enter access token
`rfq-local-preview` and connect. This launcher binds only to this computer,
uses `data/rfq_dashboard_preview.db`, and seeds four clearly fictional sample offers.
It never changes the normal supplier database or calls paid integrations.

1. Select the demo RFQ to see complete, missing-MOQ, material-mismatch and
   missing-delivery-terms examples, including mixed currencies.
2. Create an RFQ with a quantity, unit and `name = value` specification lines.
3. Add a supplier quote. Preserve the original reply, manually enter confirmed
   fields and paste exact source excerpts. Leave unknowns blank.
4. Save to validate. Compare offers horizontally, inspect evidence, review and
   copy the targeted Chinese clarification draft. Nothing sends automatically.
5. Record revised offers as new entries. Reload to confirm persistence; original
   replies and prior offers remain available.

## Deploy a safe full preview

Netlify serves the HTML/JavaScript only. Its PR preview needs a backend running
the same branch; the production API does not gain these routes from a frontend
deploy. For a complete hosted preview, deploy this branch to a separate Railway
service/environment with its own persistent volume, `SUPPLIER_INTEL_DB_PATH`,
and `API_ACCESS_TOKEN`. Use the existing uvicorn start command from `DEPLOY.md`.
Do not share the production database volume. Open that service's
`/workspace/rfq.html` to use frontend and API together without CORS configuration.
For Netlify's frontend preview, add its exact HTTPS origin to the preview API's
`ALLOWED_ORIGINS`, then connect using the preview API address and token.

Schema migration 35 creates `rfqs` and `rfq_workspace_quotes` plus an RFQ index; it leaves
existing supplier tables and features intact. Startup applies migrations using
the existing migration runner. Take the usual database backup before eventual
production rollout. This feature branch must be functionally approved before
merging or deploying it to production.

## Scope and validation

Manual quote transcription is implemented. Automated extraction from PDF/Excel,
currency conversion, landed-cost calculations and outbound supplier messaging
are not implemented. An evidence excerpt must occur in the saved reply; a human
still needs to check that it supports the entered value. A complete offer is not
a guarantee of supplier reliability or matching commercial bases across offers.
The table keeps original currencies, Incoterms, ports, quantities and deviations
visible and never declares a cheapest supplier across incompatible terms.

```sh
python -m pytest tests/test_rfq_api.py tests/test_quote_validation.py -q
python -m playwright install chromium
python -m pytest tests/test_rfq_browser.py -q
```

The browser test creates an RFQ, records four offers through the actual UI,
checks missing fields and Chinese drafts, reloads saved records, checks a mobile
viewport and verifies supplier input is rendered as text. API tests cover auth,
fail-closed configuration, source evidence, deviations, persistence and migration
from a database with existing supplier records.
