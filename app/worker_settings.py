from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.jobs import advance_generation, requeue_open, sweep_timeouts


async def startup(ctx) -> None:
    setup_logging()
    await requeue_open(ctx, force=True)


class WorkerSettings:
    functions = [advance_generation]
    cron_jobs = [cron(sweep_timeouts), cron(requeue_open)]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
