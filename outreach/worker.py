"""Single-replica opt-in Railway worker; never enabled just by importing."""
import asyncio
import logging
import os
from datetime import datetime
from customers.mailboxes import UK
from outreach.repository import OutreachRepository
from outreach.service import OutreachService

logger = logging.getLogger(__name__)


def in_window(now):
    local = now.astimezone(UK)
    return local.weekday() < 5 and (8,30) <= (local.hour,local.minute) < (17,0)


async def run(db_path):
    while True:
        await asyncio.sleep(60)
        if os.getenv('OUTREACH_SCHEDULE_ENABLED','').lower() != 'true':
            continue
        if not in_window(datetime.now(UK)):
            continue
        try:
            await asyncio.to_thread(OutreachService(OutreachRepository(db_path())).run_pending,10)
        except Exception:
            logger.exception('Outreach routine stopped; inspect mailbox/configuration before retry')
