"""Free PlanIt client. An incomplete response is never an empty success."""
import time
from datetime import timedelta
import httpx


class Inconclusive(RuntimeError):
    pass


class PlanItClient:
    def __init__(self, client=None, sleep=time.sleep, retries=2):
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), headers={"User-Agent": "SupplierIntel/1.0 (+https://ikeng.co.uk)"})
        self.sleep, self.retries = sleep, retries

    def recent(self, area, start, end):
        rows = []
        # Keep API query windows bounded (90 days); paginate every window.
        while start <= end:
            stop = min(start + timedelta(days=89), end)
            offset = 0
            while True:
                payload = self._get({'auth': area, 'start_date': start.isoformat(),
                                     'end_date': stop.isoformat(), 'pg_sz': 100, 'index': offset})
                records = payload.get('records')
                total = payload.get('total')
                if not isinstance(records, list) or not isinstance(total, int):
                    raise Inconclusive('Malformed PlanIt response')
                if payload.get('from', offset) != offset or total < 0:
                    raise Inconclusive('PlanIt returned the wrong page')
                rows.extend(records)
                offset += len(records)
                if offset >= total:
                    break
                if not records:
                    raise Inconclusive('PlanIt pagination ended before total')
            start = stop + timedelta(days=1)
        return rows

    def _get(self, params):
        for attempt in range(self.retries + 1):
            try:
                response = self.client.get('https://www.planit.org.uk/api/applics/json', params=params)
                if response.status_code == 429:
                    if attempt == self.retries:
                        raise Inconclusive('PlanIt rate limited; retry in at least five minutes')
                    retry = response.headers.get('Retry-After', '300')
                    self.sleep(max(300, int(retry) if retry.isdigit() else 300))
                    continue
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise Inconclusive('Malformed PlanIt response')
                return data
            except (httpx.HTTPError, ValueError) as exc:
                raise Inconclusive('PlanIt request failed; results unknown') from exc
