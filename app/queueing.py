import logging
import uuid

logger = logging.getLogger(__name__)

_pool = None


async def enqueue_generation(task_id: uuid.UUID) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.queue_mode == "off":
        return
    if settings.queue_mode == "inline":
        from app.jobs import advance_generation

        await advance_generation({"redis": None}, str(task_id))
        return
    pool = await _arq_pool()
    await pool.enqueue_job("advance_generation", str(task_id))


async def _arq_pool():
    global _pool
    if _pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        from app.core.config import get_settings

        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool
