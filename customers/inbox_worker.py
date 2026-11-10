"""Background mailbox check, so customer emails are picked up without anyone pressing a button.

Started from api/app.py's lifespan, next to the RFQ workflow poller. Every
CUSTOMER_INBOX_POLL_MINUTES it runs CustomerService.poll_inbox: customer replies are logged on
their enquiry (which stops pending follow-ups) and unknown senders become "possible new
enquiry" leads in the Customers tab. Read-only towards the outside world: it never sends.
Re-reading the same mail is harmless (activities and leads are unique per message id).
"""
import asyncio
import logging

from customers.customer_service import CustomerService
from storage.customer_repository import CustomerRepository

logger = logging.getLogger(__name__)

POLL_SINCE_DAYS = 3
POLL_LIMIT = 100


def tick(service):
    return service.poll_inbox(since_days=POLL_SINCE_DAYS, limit=POLL_LIMIT)


async def run(get_db_path, interval_minutes):
    if interval_minutes <= 0:
        return
    while True:
        await asyncio.sleep(interval_minutes * 60)
        try:
            result = await asyncio.to_thread(tick, CustomerService(CustomerRepository(get_db_path())))
            for address in result.get('mailbox_errors', []):
                logger.warning('customer inbox check failed for mailbox %s; will retry', address)
            if result['logged'] or result['possible_new_enquiries']:
                logger.info('customer inbox: %d replies logged, %d possible new enquiries',
                            result['logged'], result['possible_new_enquiries'])
        except Exception as exc:  # mail not configured, Zoho down: log and try again next interval
            logger.warning('customer inbox check failed, will retry: %s', type(exc).__name__)
