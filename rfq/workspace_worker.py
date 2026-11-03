"""Persistent policies drive polling even after the user's browser closes."""
import asyncio
import logging

from rfq.workspace_workflow import WorkspaceWorkflow
from storage.rfq_workflow_repository import WorkflowRepository

logger=logging.getLogger(__name__)


def tick(db_path):
    for link in WorkflowRepository(db_path).due():
        WorkspaceWorkflow(db_path).sync(link['rfq_id'])


async def run(get_db_path):
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(tick,get_db_path())
        except Exception:
            logger.error('RFQ workflow scheduler failed; check service configuration')
