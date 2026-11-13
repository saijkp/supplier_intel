"""Free PlanIt client. An incomplete response is never an empty success."""
import time
from datetime import timedelta
import httpx


class Inconclusive(RuntimeError):
    pass


class RateLimited(Inconclusive):
    pass


class PlanItClient:
    def __init__(self, client=None, sleep=time.sleep, retries=2):
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15), headers={"User-Agent": "SupplierIntel/1.0 (+https://ikeng.co.uk)"})
        self.sleep, self.retries = sleep, retries

    def recent(self, area, start, end):
        # PlanIt filters are ANDed. Fetch two independent windows and union;
        # otherwise old validations decided recently disappear from the feed.
        rows = {}
        deadline = getattr(self, 'deadline', None) or time.monotonic() + 2400
        while start <= end:
            stop = min(start + timedelta(days=89), end)
            for lower, upper in [('start_date', 'end_date'), ('decided_start', 'decided_end')]:
                offset = 0
                while True:
                    if time.monotonic() > deadline:
                        raise Inconclusive('Planning job budget exceeded; results inconclusive')
                    payload = self._get({'auth': area, lower: start.isoformat(),
                                         upper: stop.isoformat(), 'pg_sz': 100, 'index': offset})
                    records, total = payload.get('records'), payload.get('total')
                    if payload.get('error'):
                        raise Inconclusive('PlanIt error response; results unknown')
                    if isinstance(records, list) and not records and total is None and offset == 0:
                        break
                    if not isinstance(records, list) or type(total) is not int or total < len(records):
                        raise Inconclusive('Malformed PlanIt response')
                    if payload.get('from', offset) != offset or total < 0:
                        raise Inconclusive('PlanIt returned the wrong page')
                    for record in records:
                        if not isinstance(record, dict) or not (record.get('reference') or record.get('uid')):
                            raise Inconclusive('Malformed PlanIt application')
                        rows.setdefault(record.get('reference') or record['uid'], record)
                    offset += len(records)
                    if offset >= total:
                        break
                    if not records:
                        raise Inconclusive('PlanIt pagination ended before total')
            start = stop + timedelta(days=1)
        return list(rows.values())

    def _get(self, params):
        for attempt in range(self.retries + 1):
            try:
                response = self.client.get('https://www.planit.org.uk/api/applics/json', params=params)
                if response.status_code == 429:
                    raise RateLimited('PlanIt rate limited; batch stopped, retry later')
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise Inconclusive('Malformed PlanIt response')
                return data
            except (httpx.HTTPError, ValueError) as exc:
                raise Inconclusive('PlanIt request failed; results unknown') from exc
