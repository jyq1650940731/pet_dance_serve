import logging
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import start_of_today_cn, utcnow
from app.core.errors import AppError, StorageNotFound
from app.domain.constants import PAGE_SIZE
from app.models import AuditLog, GenerationTask, StaffUser, Template
from app.services.common import new_id
from app.storage import Storage, safe_key

logger = logging.getLogger(__name__)


def add_audit(
    session: AsyncSession,
    staff: StaffUser,
    action: str,
    object_id: uuid.UUID,
    reason: str | None = None,
) -> None:
    session.add(
        AuditLog(
            id=new_id(),
            staff_id=staff.id,
            action=action,
            object_type="template",
            object_id=str(object_id),
            reason=reason,
        )
    )


def _check_key(key: str | None, prefix: str) -> None:
    if key is None:
        return
    safe_key(key)
    if not key.startswith(prefix):
        raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")


async def ensure_publishable(template: Template, storage: Storage) -> None:
    if not template.cover_key or not template.preview_key:
        raise AppError(400, "TEMPLATE_INCOMPLETE", "上架前需要封面和预览片")
    if not template.prompt.strip() or not template.negative_prompt.strip():
        raise AppError(400, "TEMPLATE_INCOMPLETE", "上架前需要提示词")
    cover = await storage.head(template.cover_key)
    preview = await storage.head(template.preview_key)
    if cover is None or preview is None:
        raise AppError(400, "TEMPLATE_INCOMPLETE", "封面或预览片文件不存在")
    if not (cover.content_type or "").startswith("image/"):
        raise AppError(400, "TEMPLATE_INCOMPLETE", "封面需要是图片")
    try:
        data = await storage.get_bytes(template.preview_key)
    except StorageNotFound as exc:
        raise AppError(400, "TEMPLATE_INCOMPLETE", "预览片文件不存在") from exc
    if b"ftyp" not in data[:64]:
        raise AppError(400, "TEMPLATE_INCOMPLETE", "预览片需要是 MP4")


def _apply_keys(template: Template) -> None:
    _check_key(template.cover_key, "templates/covers/")
    _check_key(template.preview_key, "templates/previews/")


async def create_template(session: AsyncSession, storage: Storage, staff: StaffUser, data: dict) -> Template:
    template = Template(
        id=new_id(),
        title=data["title"],
        subtitle=data.get("subtitle") or "",
        category=data["category"],
        status=data.get("status") or "draft",
        sort=data.get("sort") or 0,
        duration_sec=data.get("duration_sec") or 5,
        aspect_ratio="9:16",
        credit_cost=data.get("credit_cost") or 10,
        pet_scope=data["pet_scope"],
        provider=data.get("provider") or "kling",
        model_name=data.get("model") or "kling-v2.5-turbo",
        prompt=data["prompt"],
        negative_prompt=data["negative_prompt"],
        audio_asset_id=data.get("audio_asset_id"),
        cover_key=data.get("cover_key"),
        preview_key=data.get("preview_key"),
    )
    _apply_keys(template)
    session.add(template)
    await session.flush()
    if template.status == "published":
        await ensure_publishable(template, storage)
    add_audit(session, staff, "template.create", template.id)
    return template


async def update_template(session: AsyncSession, storage: Storage, staff: StaffUser, template: Template, data: dict) -> Template:
    previous = template.status
    mapping = {
        "title": "title",
        "subtitle": "subtitle",
        "category": "category",
        "duration_sec": "duration_sec",
        "credit_cost": "credit_cost",
        "pet_scope": "pet_scope",
        "provider": "provider",
        "model": "model_name",
        "prompt": "prompt",
        "negative_prompt": "negative_prompt",
        "audio_asset_id": "audio_asset_id",
        "cover_key": "cover_key",
        "preview_key": "preview_key",
        "sort": "sort",
        "status": "status",
    }
    for key, attr in mapping.items():
        if key in data:
            setattr(template, attr, data[key])
    template.aspect_ratio = "9:16"
    template.updated_at = utcnow()
    _apply_keys(template)
    if template.status == "published":
        await ensure_publishable(template, storage)
    if template.status != previous and template.status == "published":
        action = "template.publish"
    elif template.status != previous and template.status == "unpublished":
        action = "template.unpublish"
    else:
        action = "template.update"
    add_audit(session, staff, action, template.id)
    return template


def _copy_title(title: str) -> str:
    suffix = " 副本"
    if len(title) + len(suffix) <= 40:
        return title + suffix
    return title[: 40 - len(suffix)] + suffix


async def duplicate_template(session: AsyncSession, storage: Storage, staff: StaffUser, source: Template) -> Template:
    copy_id = new_id()
    cover_key = None
    preview_key = None
    try:
        if source.cover_key:
            ext = Path(source.cover_key).suffix or ".jpg"
            cover_key = f"templates/covers/{copy_id}{ext}"
            await storage.copy_object(source.cover_key, cover_key)
        if source.preview_key:
            ext = Path(source.preview_key).suffix or ".mp4"
            preview_key = f"templates/previews/{copy_id}{ext}"
            await storage.copy_object(source.preview_key, preview_key)
    except StorageNotFound as exc:
        raise AppError(400, "TEMPLATE_INCOMPLETE", "原模板的封面或预览片不存在") from exc
    copy = Template(
        id=copy_id,
        title=_copy_title(source.title),
        subtitle=source.subtitle,
        category=source.category,
        status="draft",
        sort=source.sort + 1,
        duration_sec=source.duration_sec,
        aspect_ratio="9:16",
        credit_cost=source.credit_cost,
        pet_scope=source.pet_scope,
        provider=source.provider,
        model_name=source.model_name,
        prompt=source.prompt,
        negative_prompt=source.negative_prompt,
        audio_asset_id=source.audio_asset_id,
        cover_key=cover_key,
        preview_key=preview_key,
    )
    session.add(copy)
    await session.flush()
    add_audit(session, staff, "template.duplicate", copy.id, reason=f"复制自 {source.id}")
    return copy


async def today_count(session: AsyncSession, template_id: uuid.UUID) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(GenerationTask)
        .where(GenerationTask.template_id == template_id, GenerationTask.created_at >= start_of_today_cn())
    )
    return int(value or 0)


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> Template:
    template = await session.get(Template, template_id)
    if template is None:
        raise AppError(404, "NOT_FOUND", "模板不存在")
    return template


async def list_admin_templates(
    session: AsyncSession,
    *,
    page: int,
    category: str | None,
    status: str | None,
) -> tuple[list[Template], int, dict[uuid.UUID, int]]:
    filters = []
    if category:
        filters.append(Template.category == category)
    if status:
        filters.append(Template.status == status)
    total = int(await session.scalar(select(func.count()).select_from(Template).where(*filters)) or 0)
    rows = (
        await session.execute(
            select(Template)
            .where(*filters)
            .order_by(Template.sort.asc(), Template.created_at.asc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).scalars().all()
    counts: dict[uuid.UUID, int] = {}
    ids = [row.id for row in rows]
    if ids:
        grouped = await session.execute(
            select(GenerationTask.template_id, func.count())
            .where(GenerationTask.template_id.in_(ids), GenerationTask.created_at >= start_of_today_cn())
            .group_by(GenerationTask.template_id)
        )
        counts = {template_id: int(count) for template_id, count in grouped.all()}
    return list(rows), total, counts


async def list_public_templates(
    session: AsyncSession, *, page: int, category: str | None
) -> tuple[list[Template], int, uuid.UUID | None]:
    filters = [Template.status == "published"]
    if category:
        filters.append(Template.category == category)
    total = int(await session.scalar(select(func.count()).select_from(Template).where(*filters)) or 0)
    rows = (
        await session.execute(
            select(Template)
            .where(*filters)
            .order_by(Template.sort.asc(), Template.created_at.asc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).scalars().all()
    featured = await session.scalar(
        select(Template.id)
        .where(Template.status == "published")
        .order_by(Template.sort.asc(), Template.created_at.asc())
        .limit(1)
    )
    return list(rows), total, featured


async def get_published_template(session: AsyncSession, template_id: uuid.UUID) -> Template:
    template = await session.get(Template, template_id)
    if template is None or template.status != "published":
        raise AppError(404, "NOT_FOUND", "模板不存在")
    return template
