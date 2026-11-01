import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import locked
from app.core.errors import AppError
from app.models import CreditAccount, CreditLedger


async def lock_account(session: AsyncSession, user_id: uuid.UUID) -> CreditAccount:
    stmt = locked(session, select(CreditAccount).where(CreditAccount.user_id == user_id))
    account = (await session.execute(stmt)).scalar_one()
    return account


async def apply_ledger(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    entry_type: str,
    amount: int,
    frozen_delta: int,
    task_id: uuid.UUID | None = None,
    reason: str | None = None,
    staff_id: uuid.UUID | None = None,
) -> CreditLedger:
    account = await lock_account(session, user_id)
    next_balance = account.balance + amount
    next_frozen = account.frozen + frozen_delta
    if next_balance < 0 or next_frozen < 0:
        raise AppError(409, "INSUFFICIENT_CREDITS", "点数不足")
    account.balance = next_balance
    account.frozen = next_frozen
    row = CreditLedger(
        id=uuid.uuid4(),
        user_id=user_id,
        entry_type=entry_type,
        amount=amount,
        frozen_delta=frozen_delta,
        balance_after=next_balance,
        frozen_after=next_frozen,
        task_id=task_id,
        reason=reason,
        staff_id=staff_id,
    )
    session.add(row)
    await session.flush()
    return row
