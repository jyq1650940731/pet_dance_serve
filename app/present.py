import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.constants import public_error, stage_label
from app.domain.phones import mask_phone
from app.models import CreditAccount, GenerationTask, Media, Template, User
from app.schemas import AdminTemplateOut, CreditOut, GenerationOut, TemplateOut, UserOut
from app.services.accounts import successful_count
from app.storage import Storage


async def build_user(session: AsyncSession, user: User) -> UserOut:
    account = await session.get(CreditAccount, user.id)
    balance = account.balance if account else 0
    frozen = account.frozen if account else 0
    return UserOut(
        id=user.id,
        phone_masked=mask_phone(user.phone),
        pet_type=user.pet_type,
        status=user.status,
        credits=CreditOut(balance=balance, frozen=frozen),
        successful_count=await successful_count(session, user.id),
    )


async def build_template(storage: Storage, template: Template) -> TemplateOut:
    return TemplateOut(
        id=template.id,
        title=template.title,
        subtitle=template.subtitle,
        category=template.category,
        cover_url=await storage.public_url(template.cover_key) if template.cover_key else None,
        preview_url=await storage.public_url(template.preview_key) if template.preview_key else None,
        duration_sec=template.duration_sec,
        aspect_ratio=template.aspect_ratio,
        credit_cost=template.credit_cost,
        pet_scope=template.pet_scope,
        sort=template.sort,
    )


async def build_admin_template(storage: Storage, template: Template, today_count: int) -> AdminTemplateOut:
    public = await build_template(storage, template)
    return AdminTemplateOut(
        **public.model_dump(),
        status=template.status,
        provider=template.provider,
        model=template.model_name,
        prompt=template.prompt,
        negative_prompt=template.negative_prompt,
        audio_asset_id=template.audio_asset_id,
        cover_key=template.cover_key,
        preview_key=template.preview_key,
        today_count=today_count,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


async def build_generation(
    storage: Storage,
    task: GenerationTask,
    template: Template,
    media: Media | None,
) -> GenerationOut:
    playable = False
    video_url = None
    message = None
    if task.status == "failed":
        message = public_error(task.error_code)
    elif task.status == "succeeded":
        if media is not None and media.taken_down:
            message = "该作品已下架"
        elif task.purged_at is not None or media is None or media.deleted_at is not None or not media.object_key:
            message = "该作品已清理"
        else:
            playable = True
            video_url = await storage.presign_get(media.object_key)
    cover_url = await storage.public_url(template.cover_key) if template.cover_key else None
    return GenerationOut(
        id=task.id,
        status=task.status,
        stage_label=stage_label(task.status),
        template_id=template.id,
        template_title=template.title,
        duration_sec=template.duration_sec,
        credit_cost=task.credit_cost,
        error_code=task.error_code,
        error_message=message,
        playable=playable,
        video_url=video_url,
        cover_url=cover_url,
        created_at=task.created_at,
        finished_at=task.finished_at,
    )


def parse_idempotency_key(value: str | None) -> str | None:
    import re

    from app.core.errors import AppError

    if value is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise AppError(422, "VALIDATION", "Idempotency-Key 格式不正确")
    return value


async def media_for(task: GenerationTask, mapping: dict[uuid.UUID, Media]) -> Media | None:
    if task.result_media_id is None:
        return None
    return mapping.get(task.result_media_id)
