import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting


async def get_settings_row(session: AsyncSession) -> AppSetting:
    row = await session.get(AppSetting, 1)
    if row is None:
        row = AppSetting(id=1, gift_credits=20, maintenance=False, maintenance_message="")
        session.add(row)
        await session.flush()
    return row


def remember_job(session: AsyncSession, callback) -> None:
    session.info.setdefault("post_commit", []).append(callback)


def new_id() -> uuid.UUID:
    return uuid.uuid4()
