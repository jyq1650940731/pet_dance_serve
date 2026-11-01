import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import (
    dummy_password_hash,
    encode_access_token,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.domain.constants import TERMINAL_STATUSES
from app.models import ConsentLog, CreditAccount, GenerationTask, GiftedPhone, RefreshToken, StaffUser, User
from app.services.common import get_settings_row, new_id
from app.services.credits import apply_ledger
from app.sms import verify_code

logger = logging.getLogger(__name__)


async def authenticate_staff(session: AsyncSession, username: str, password: str) -> StaffUser:
    staff = await session.scalar(select(StaffUser).where(StaffUser.username == username.strip()))
    password_hash = staff.password_hash if staff else dummy_password_hash()
    if not staff or not verify_password(password, password_hash) or staff.status != "active":
        raise AppError(401, "UNAUTHORIZED", "用户名或密码错误")
    staff.last_login_at = utcnow()
    return staff


async def issue_session(session: AsyncSession, *, subject_type: str, subject_id: uuid.UUID, role: str | None = None) -> tuple[str, str, int]:
    audience = "app" if subject_type == "user" else "admin"
    access, expires_in = encode_access_token(subject_id=subject_id, audience=audience, role=role)
    raw, digest = new_refresh_token()
    settings = get_settings()
    ttl = settings.app_refresh_ttl_sec if subject_type == "user" else settings.admin_refresh_ttl_sec
    session.add(
        RefreshToken(
            id=new_id(),
            subject_type=subject_type,
            subject_id=subject_id,
            token_hash=digest,
            expires_at=utcnow() + timedelta(seconds=ttl),
        )
    )
    return access, raw, expires_in


async def refresh_session(session: AsyncSession, raw_token: str, subject_type: str) -> tuple[str, str, int]:
    token_hash = hash_refresh_token(raw_token)
    row = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if row is None or row.revoked_at is not None or row.subject_type != subject_type or row.expires_at < utcnow():
        raise AppError(401, "UNAUTHORIZED", "登录已失效")
    if subject_type == "user":
        user = await session.get(User, row.subject_id)
        if user is None or user.status == "deleted":
            raise AppError(401, "UNAUTHORIZED", "登录已失效")
        if user.status == "banned":
            row.revoked_at = utcnow()
            raise AppError(403, "ACCOUNT_BANNED", "账号已封禁")
        access, expires_in = encode_access_token(subject_id=user.id, audience="app")
    else:
        staff = await session.get(StaffUser, row.subject_id)
        if staff is None or staff.status != "active":
            row.revoked_at = utcnow()
            raise AppError(401, "UNAUTHORIZED", "登录已失效")
        access, expires_in = encode_access_token(subject_id=staff.id, audience="admin", role=staff.role)
    return access, raw_token, expires_in


async def revoke_refresh_token(session: AsyncSession, raw_token: str, subject_type: str) -> None:
    token_hash = hash_refresh_token(raw_token)
    row = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if row is None or row.subject_type != subject_type:
        return
    if row.revoked_at is None:
        row.revoked_at = utcnow()


async def login_with_sms(
    session: AsyncSession,
    redis,
    *,
    phone: str,
    code: str,
    terms_version: str,
    privacy_version: str,
    pet_type: str | None,
) -> tuple[User, bool]:
    settings = get_settings()
    if terms_version != settings.terms_version or privacy_version != settings.privacy_version:
        raise AppError(409, "CONSENT_OUTDATED", "请重新阅读并同意最新的用户协议和隐私政策")
    await verify_code(redis, phone, code)
    user = await session.scalar(select(User).where(User.phone == phone))
    gifted = False
    if user is None:
        user = await _create_user(session, phone, pet_type)
        if user.status == "banned":
            raise AppError(403, "ACCOUNT_BANNED", "账号已封禁")
        if user.status != "active":
            raise AppError(401, "UNAUTHORIZED", "登录已失效")
        gifted = await _grant_gift(session, user)
    else:
        if user.status == "banned":
            raise AppError(403, "ACCOUNT_BANNED", "账号已封禁")
        if user.status != "active":
            raise AppError(401, "UNAUTHORIZED", "登录已失效")
        if pet_type is not None:
            user.pet_type = pet_type
    session.add(
        ConsentLog(
            id=new_id(),
            user_id=user.id,
            terms_version=terms_version,
            privacy_version=privacy_version,
        )
    )
    return user, gifted


async def _create_user(session: AsyncSession, phone: str, pet_type: str | None) -> User:
    user = User(id=new_id(), phone=phone, status="active", pet_type=pet_type)
    session.add(user)
    await session.flush()
    session.add(CreditAccount(user_id=user.id, balance=0, frozen=0))
    await session.flush()
    return user


async def _grant_gift(session: AsyncSession, user: User) -> bool:
    if not user.phone:
        return False
    row = await get_settings_row(session)
    existing = await session.get(GiftedPhone, user.phone)
    if existing is not None:
        return False
    session.add(GiftedPhone(phone=user.phone, user_id=user.id))
    await session.flush()
    if row.gift_credits <= 0:
        return False
    await apply_ledger(
        session,
        user.id,
        entry_type="gift",
        amount=row.gift_credits,
        frozen_delta=0,
        reason="新用户赠送",
    )
    return True


async def successful_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(GenerationTask)
        .where(
            GenerationTask.user_id == user_id,
            GenerationTask.status == "succeeded",
            GenerationTask.deleted_at.is_(None),
        )
    )
    return int(value or 0)


async def delete_account(session: AsyncSession, storage, user: User) -> None:
    from app.services.generations import fail_task, purge_task_files

    tasks = (
        await session.execute(select(GenerationTask).where(GenerationTask.user_id == user.id, GenerationTask.deleted_at.is_(None)))
    ).scalars().all()
    for task in tasks:
        if task.status not in TERMINAL_STATUSES:
            await fail_task(session, task, "CANCELLED")
        task.deleted_at = utcnow()
        await purge_task_files(session, storage, task)
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.subject_type == "user",
            RefreshToken.subject_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    for prefix in (f"staging/{user.id}", f"originals/{user.id}", f"results/{user.id}"):
        try:
            await storage.delete_prefix(prefix)
        except Exception:
            logger.exception("清理用户文件失败")
    user.status = "deleted"
    user.phone = None
    user.pet_type = None
    user.deleted_at = utcnow()
