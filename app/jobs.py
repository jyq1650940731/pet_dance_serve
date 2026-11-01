import logging
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import session_scope
from app.domain.constants import ACTIVE_STATUSES, TERMINAL_STATUSES
from app.models import GenerationTask
from app.services.generations import step_once

logger = logging.getLogger(__name__)


async def advance_generation(ctx, task_id: str) -> str:
    delay = get_settings().placeholder_step_seconds
    inline = get_settings().queue_mode == "inline" or ctx.get("redis") is None
    status = "queued"
    for _ in range(8):
        status = await step_once(uuid.UUID(task_id))
        if status in TERMINAL_STATUSES:
            return status
        if inline or delay <= 0:
            continue
        redis = ctx["redis"]
        await redis.enqueue_job("advance_generation", task_id, _defer_by=timedelta(seconds=delay))
        return status
    logger.error("任务 %s 没有在预期步数内结束", task_id)
    return status


async def sweep_timeouts(ctx) -> None:
    del ctx
    timeout = timedelta(seconds=get_settings().generation_timeout_sec)
    cutoff = utcnow() - timeout
    async with session_scope() as session:
        ids = (
            await session.execute(
                select(GenerationTask.id).where(
                    GenerationTask.status.in_(ACTIVE_STATUSES),
                    GenerationTask.deleted_at.is_(None),
                    GenerationTask.queued_at < cutoff,
                )
            )
        ).scalars().all()
    for task_id in ids:
        await step_once(task_id)


async def requeue_open(ctx, *, force: bool = False) -> None:
    cutoff = None if force else utcnow() - timedelta(seconds=15)
    async with session_scope() as session:
        stmt = select(GenerationTask.id).where(
            GenerationTask.status.in_(ACTIVE_STATUSES),
            GenerationTask.deleted_at.is_(None),
        )
        if cutoff is not None:
            stmt = stmt.where(GenerationTask.updated_at < cutoff)
        ids = (await session.execute(stmt)).scalars().all()
    redis = ctx.get("redis")
    if redis is None:
        return
    for task_id in ids:
        await redis.enqueue_job("advance_generation", str(task_id))
