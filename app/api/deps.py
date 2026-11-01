import logging
import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.core.errors import AppError
from app.core.security import decode_access_token
from app.models import StaffUser, User
from app.storage import Storage

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


async def get_session():
    session = get_sessionmaker()()
    jobs: list = []
    try:
        yield session
        jobs = list(session.info.get("post_commit", []))
        await session.commit()
    except Exception:
        try:
            await session.rollback()
        except Exception:
            logger.exception("回滚失败")
        raise
    finally:
        await session.close()
    for job in jobs:
        try:
            result = job()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.exception("提交后的任务入队失败")


def get_storage_dep(request: Request) -> Storage:
    return request.app.state.storage


def get_redis(request: Request):
    return request.app.state.redis


async def _token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise AppError(401, "UNAUTHORIZED", "请先登录")
    return creds.credentials


async def _user(session: AsyncSession, token: str, *, allow_banned: bool) -> User:
    try:
        payload = decode_access_token(token, "app")
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise AppError(401, "UNAUTHORIZED", "登录已失效") from exc
    user = await session.get(User, user_id)
    if user is None or user.status == "deleted":
        raise AppError(401, "UNAUTHORIZED", "登录已失效")
    if user.status == "banned" and not allow_banned:
        raise AppError(403, "ACCOUNT_BANNED", "账号已封禁")
    if user.status not in {"active", "banned"}:
        raise AppError(401, "UNAUTHORIZED", "登录已失效")
    return user


async def get_current_user(session: AsyncSession = Depends(get_session), token: str = Depends(_token)) -> User:
    return await _user(session, token, allow_banned=False)


async def get_user_for_deletion(session: AsyncSession = Depends(get_session), token: str = Depends(_token)) -> User:
    return await _user(session, token, allow_banned=True)


async def get_current_staff(session: AsyncSession = Depends(get_session), token: str = Depends(_token)) -> StaffUser:
    try:
        payload = decode_access_token(token, "admin")
        staff_id = uuid.UUID(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise AppError(401, "UNAUTHORIZED", "登录已失效") from exc
    staff = await session.get(StaffUser, staff_id)
    if staff is None or staff.status != "active":
        raise AppError(401, "UNAUTHORIZED", "登录已失效")
    return staff
