import logging
import uuid
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import locked, session_scope
from app.core.errors import AppError, StorageNotFound
from app.domain.constants import (
    ACTIVE_STATUSES,
    MAX_IMAGE_BYTES,
    NEXT_STATUS,
    PAGE_SIZE,
    STATUS_STAMP,
    TERMINAL_STATUSES,
    assert_transition,
)
from app.models import GenerationTask, Media, Template
from app.queueing import enqueue_generation
from app.services.common import get_settings_row, new_id, remember_job
from app.services.credits import apply_ledger, lock_account
from app.services.images import validate_pet_image
from app.storage import Storage, get_storage, norm_content_type, safe_key

logger = logging.getLogger(__name__)


async def create_generation(
    session: AsyncSession,
    storage: Storage,
    *,
    user_id: uuid.UUID,
    template_id: uuid.UUID,
    image_key: str,
    ownership_confirmed: bool,
    idempotency_key: str | None,
) -> GenerationTask:
    if not ownership_confirmed:
        raise AppError(400, "OWNERSHIP_REQUIRED", "请确认对照片有使用权")
    settings_row = await get_settings_row(session)
    if settings_row.maintenance:
        message = settings_row.maintenance_message or "系统维护中，暂时不能生成"
        raise AppError(503, "MAINTENANCE", message)

    safe_key(image_key)
    prefix = f"staging/{user_id}/"
    if not image_key.startswith(prefix):
        raise AppError(400, "IMAGE_INVALID", "请重新上传照片")

    await lock_account(session, user_id)
    if idempotency_key:
        existing = await session.scalar(
            select(GenerationTask).where(
                GenerationTask.user_id == user_id,
                GenerationTask.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing

    template = await session.get(Template, template_id)
    if template is None or template.status != "published":
        raise AppError(409, "TEMPLATE_UNAVAILABLE", "模板已下架")

    info = await storage.head(image_key)
    if info is None or info.size <= 0 or info.size > MAX_IMAGE_BYTES:
        raise AppError(400, "IMAGE_INVALID", "请重新上传照片")
    if info.content_type and norm_content_type(info.content_type) != "image/jpeg":
        raise AppError(400, "IMAGE_INVALID", "图片需为 9:16 的 JPEG，长边不超过 1280，大小不超过 1.5MB")
    try:
        data = await storage.get_bytes(image_key)
    except StorageNotFound as exc:
        raise AppError(400, "IMAGE_INVALID", "请重新上传照片") from exc
    validate_pet_image(data)

    task_id = new_id()
    original_key = f"originals/{user_id}/{task_id}.jpg"
    stored = False
    try:
        await storage.put_bytes(original_key, data, "image/jpeg")
        stored = True
        media = Media(
            id=new_id(),
            user_id=user_id,
            kind="original",
            object_key=original_key,
            content_type="image/jpeg",
            size=len(data),
        )
        session.add(media)
        await session.flush()
        task = GenerationTask(
            id=task_id,
            user_id=user_id,
            template_id=template.id,
            original_media_id=media.id,
            status="queued",
            credit_cost=template.credit_cost,
            idempotency_key=idempotency_key,
            ownership_confirmed=True,
            queued_at=utcnow(),
        )
        session.add(task)
        await session.flush()
        ledger = await apply_ledger(
            session,
            user_id,
            entry_type="freeze",
            amount=-template.credit_cost,
            frozen_delta=template.credit_cost,
            task_id=task.id,
            reason="生成冻结",
        )
        task.freeze_ledger_id = ledger.id
        await session.flush()
    except Exception:
        if stored:
            try:
                await storage.delete(original_key)
            except Exception:
                logger.exception("回滚原图失败")
        raise
    try:
        await storage.delete(image_key)
    except Exception:
        logger.exception("清理临时照片失败")
    remember_job(session, lambda: enqueue_generation(task_id))
    return task


async def settle_success(session: AsyncSession, task: GenerationTask) -> None:
    if task.settled:
        return
    await apply_ledger(
        session,
        task.user_id,
        entry_type="consume",
        amount=0,
        frozen_delta=-task.credit_cost,
        task_id=task.id,
        reason="生成成功",
    )
    task.settled = True


async def settle_refund(session: AsyncSession, task: GenerationTask, reason: str) -> None:
    if task.settled:
        return
    if task.freeze_ledger_id is None:
        task.settled = True
        return
    await apply_ledger(
        session,
        task.user_id,
        entry_type="refund",
        amount=task.credit_cost,
        frozen_delta=-task.credit_cost,
        task_id=task.id,
        reason=reason,
    )
    task.settled = True


async def fail_task(session: AsyncSession, task: GenerationTask, code: str, detail: str = "") -> None:
    if task.status in TERMINAL_STATUSES:
        return
    assert_transition(task.status, "failed")
    now = utcnow()
    task.status = "failed"
    task.error_code = code
    task.error_detail = (detail or "")[:2000] or None
    task.finished_at = now
    task.updated_at = now
    await settle_refund(session, task, reason=code)
    logger.info("任务 %s 失败 %s", task.id, code)


async def purge_task_files(session: AsyncSession, storage: Storage, task: GenerationTask) -> None:
    media_ids = [item for item in (task.original_media_id, task.result_media_id) if item]
    if not media_ids:
        return
    medias = (await session.execute(select(Media).where(Media.id.in_(media_ids)))).scalars().all()
    now = utcnow()
    for media in medias:
        if media.deleted_at is None and media.object_key:
            try:
                await storage.delete(media.object_key)
            except Exception:
                logger.exception("清理文件失败 %s", media.id)
        media.deleted_at = now


async def enforce_retention(session: AsyncSession, storage: Storage, user_id: uuid.UUID) -> None:
    limit = get_settings().works_retention
    rows = (
        await session.execute(
            select(GenerationTask)
            .where(
                GenerationTask.user_id == user_id,
                GenerationTask.status == "succeeded",
                GenerationTask.deleted_at.is_(None),
                GenerationTask.purged_at.is_(None),
            )
            .order_by(GenerationTask.created_at.asc())
        )
    ).scalars().all()
    extra = len(rows) - limit
    if extra <= 0:
        return
    now = utcnow()
    for old in rows[:extra]:
        await purge_task_files(session, storage, old)
        old.purged_at = now


async def _placeholder_video(session: AsyncSession, storage: Storage, task: GenerationTask) -> None:
    template = await session.get(Template, task.template_id)
    if template is None or not template.preview_key:
        await fail_task(session, task, "PREVIEW_MISSING", "模板没有预览片")
        return
    try:
        data = await storage.get_bytes(template.preview_key)
        info = await storage.head(template.preview_key)
    except StorageNotFound:
        await fail_task(session, task, "PREVIEW_MISSING", "预览片文件不存在")
        return
    except Exception as exc:
        await fail_task(session, task, "MUX_FAILED", type(exc).__name__)
        return
    result_key = f"results/{task.user_id}/{task.id}.mp4"
    content_type = (info.content_type if info and info.content_type else None) or "video/mp4"
    await storage.put_bytes(result_key, data, content_type)
    media = Media(
        id=new_id(),
        user_id=task.user_id,
        kind="result",
        object_key=result_key,
        content_type=content_type,
        size=len(data),
    )
    session.add(media)
    await session.flush()
    task.result_media_id = media.id
    task.attempt_count += 1
    assert_transition(task.status, "muxing")
    task.status = "muxing"
    task.muxing_at = utcnow()
    task.updated_at = task.muxing_at


async def advance_task(session: AsyncSession, storage: Storage, task: GenerationTask) -> str:
    if task.status in TERMINAL_STATUSES:
        return task.status
    now = utcnow()
    task.updated_at = now
    if task.deleted_at is not None:
        await fail_task(session, task, "CANCELLED")
        return task.status
    timeout = timedelta(seconds=get_settings().generation_timeout_sec)
    if now - task.queued_at >= timeout:
        await fail_task(session, task, "TIMEOUT")
        return task.status
    if task.status == "generating":
        await _placeholder_video(session, storage, task)
        return task.status
    if task.status == "muxing":
        assert_transition(task.status, "succeeded")
        task.status = "succeeded"
        task.finished_at = now
        await settle_success(session, task)
        await enforce_retention(session, storage, task.user_id)
        logger.info("任务 %s 成功", task.id)
        return task.status
    destination = NEXT_STATUS[task.status]
    assert_transition(task.status, destination)
    task.status = destination
    setattr(task, STATUS_STAMP[destination], now)
    logger.info("任务 %s 进入 %s", task.id, destination)
    return task.status


async def step_once(task_id: uuid.UUID) -> str:
    storage = get_storage()
    async with session_scope() as session:
        stmt = locked(session, select(GenerationTask).where(GenerationTask.id == task_id))
        task = (await session.execute(stmt)).scalar_one_or_none()
        if task is None:
            return "failed"
        return await advance_task(session, storage, task)


async def owned_task(session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID) -> GenerationTask:
    task = await session.get(GenerationTask, task_id)
    if task is None or task.user_id != user_id or task.deleted_at is not None:
        raise AppError(404, "NOT_FOUND", "作品不存在")
    return task


async def delete_generation(session: AsyncSession, storage: Storage, user_id: uuid.UUID, task_id: uuid.UUID) -> None:
    task = await owned_task(session, user_id, task_id)
    if task.status not in TERMINAL_STATUSES:
        await fail_task(session, task, "CANCELLED")
    task.deleted_at = utcnow()
    await purge_task_files(session, storage, task)


def _list_filters(user_id: uuid.UUID):
    return (GenerationTask.user_id == user_id, GenerationTask.deleted_at.is_(None))


async def list_generations(session: AsyncSession, user_id: uuid.UUID, page: int) -> tuple[list[tuple[GenerationTask, Template]], int]:
    filters = _list_filters(user_id)
    total = int(await session.scalar(select(func.count()).select_from(GenerationTask).where(*filters)) or 0)
    active_first = case((GenerationTask.status.in_(ACTIVE_STATUSES), 0), else_=1)
    rows = (
        await session.execute(
            select(GenerationTask, Template)
            .join(Template, Template.id == GenerationTask.template_id)
            .where(*filters)
            .order_by(active_first, GenerationTask.created_at.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    return list(rows), total


async def load_generation(session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID) -> tuple[GenerationTask, Template]:
    task = await owned_task(session, user_id, task_id)
    template = await session.get(Template, task.template_id)
    if template is None:
        raise AppError(404, "NOT_FOUND", "作品不存在")
    return task, template


async def media_map(session: AsyncSession, tasks: list[GenerationTask]) -> dict[uuid.UUID, Media]:
    ids = [task.result_media_id for task in tasks if task.result_media_id]
    if not ids:
        return {}
    rows = (await session.execute(select(Media).where(Media.id.in_(ids)))).scalars().all()
    return {row.id: row for row in rows}
