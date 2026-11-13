"""Opt-in weekly Railway planning routine; no email or paid calls."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from outreach.repository import OutreachRepository
from outreach.service import OutreachService
from planning.service import PlanningService

logger = logging.getLogger(__name__)


def due(service):
    runs = service.repo.rows('SELECT started_at FROM planning_sync_runs ORDER BY id DESC LIMIT 1')
    return not runs or service.outreach.now() - datetime.fromisoformat(runs[0]['started_at']).replace(tzinfo=timezone.utc) >= timedelta(days=7)


async def run(db_path):
    while True:
        await asyncio.sleep(60)
        try:
            service = PlanningService(OutreachService(OutreachRepository(db_path())))
            service.expire_stalled()
            if os.getenv('PLANNING_SCHEDULE_ENABLED','').lower() == 'true' and due(service):
                areas = json.loads(os.environ['PLANNING_AREAS']) if os.getenv('PLANNING_AREAS') else None
                await asyncio.to_thread(service.run_background, service.start_run(), areas=areas, days=90)
        except Exception:
            logger.exception('Planning sync failed; results inconclusive')
