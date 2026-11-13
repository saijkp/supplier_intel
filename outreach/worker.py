"""Railway lifespan worker; durable state survives processes and redeploys."""
import asyncio
import logging
import os
from datetime import datetime
from customers.mailboxes import UK
from outreach.repository import OutreachRepository
from outreach.service import OutreachService

logger = logging.getLogger(__name__)


def in_window(now):
    local=now.astimezone(UK)
    return local.weekday()<5 and (8,30)<=(local.hour,local.minute)<(17,0)


def tick(db_path):
    from outreach.always_on import AlwaysOnService
    service=OutreachService(OutreachRepository(db_path))
    # Alerts use the system transport, never the ikeng.co.uk sales identity.
    from rfq.rfq_service import RFQService
    from storage.rfq_repository import RFQRepository
    rfq=RFQService(repo=RFQRepository(db_path))
    loop=AlwaysOnService(service,alert_sender=rfq.sender.send,rfq_retry=rfq.retry_pending)
    return loop.tick()


async def run(db_path):
    while True:
        await asyncio.sleep(60)
        try:
            if os.getenv('OUTREACH_ALWAYS_ON_ENABLED','true').lower()=='true':
                await asyncio.to_thread(tick,db_path())
            elif os.getenv('OUTREACH_SCHEDULE_ENABLED','').lower()=='true' and in_window(datetime.now(UK)):
                await asyncio.to_thread(OutreachService(OutreachRepository(db_path())).run_pending,10)
        except Exception:
            logger.error('Outreach worker failed; persistent health status requires review')
