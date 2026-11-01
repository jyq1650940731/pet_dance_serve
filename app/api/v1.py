import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_redis, get_session, get_storage_dep, get_user_for_deletion
from app.core.config import get_settings
from app.core.errors import AppError
from app.domain.constants import MAX_IMAGE_BYTES, PAGE_SIZE
from app.models import Template, User
from app.present import (
    build_generation,
    build_template,
    build_user,
    media_for,
    parse_idempotency_key,
)
from app.schemas import (
    AppTokenOut,
    CreditOut,
    GenerationIn,
    GenerationListOut,
    GenerationOut,
    ImageUploadIn,
    ImageUploadOut,
    LoginIn,
    LogoutIn,
    MePatch,
    MetaOut,
    RefreshIn,
    RefreshOut,
    SmsIn,
    SmsOut,
    TemplateListOut,
    TemplateOut,
    UserOut,
)
from app.services.accounts import (
    delete_account,
    issue_session,
    login_with_sms,
    refresh_session,
    revoke_refresh_token,
)
from app.services.catalog import get_published_template, list_public_templates
from app.services.common import get_settings_row, new_id
from app.services.generations import (
    create_generation,
    delete_generation,
    list_generations,
    load_generation,
    media_map,
)
from app.sms import send_code
from app.storage import Storage

router = APIRouter(prefix="/v1")


@router.get("/meta", response_model=MetaOut)
async def meta(session: AsyncSession = Depends(get_session)) -> MetaOut:
    row = await get_settings_row(session)
    settings = get_settings()
    cheapest = await session.scalar(select(func.min(Template.credit_cost)).where(Template.status == "published"))
    return MetaOut(
        maintenance=bool(row.maintenance),
        maintenance_message=row.maintenance_message if row.maintenance else "",
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        cheapest_credit_cost=cheapest,
        gift_credits=row.gift_credits,
    )


@router.post("/auth/sms", response_model=SmsOut)
async def send_sms(body: SmsIn, redis=Depends(get_redis)) -> SmsOut:
    cooldown = await send_code(redis, body.phone)
    return SmsOut(cooldown_sec=cooldown)


@router.post("/auth/login", response_model=AppTokenOut)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session), redis=Depends(get_redis)) -> AppTokenOut:
    user, gifted = await login_with_sms(
        session,
        redis,
        phone=body.phone,
        code=body.code,
        terms_version=body.consent.terms_version,
        privacy_version=body.consent.privacy_version,
        pet_type=body.pet_type,
    )
    access, refresh, expires_in = await issue_session(session, subject_type="user", subject_id=user.id)
    return AppTokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        gifted=gifted,
        user=await build_user(session, user),
    )


@router.post("/auth/refresh", response_model=RefreshOut)
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)) -> RefreshOut:
    access, refresh_token, expires_in = await refresh_session(session, body.refresh_token, "user")
    return RefreshOut(access_token=access, refresh_token=refresh_token, expires_in=expires_in)


@router.post("/auth/logout", status_code=204)
async def logout(body: LogoutIn, session: AsyncSession = Depends(get_session)) -> Response:
    await revoke_refresh_token(session, body.refresh_token, "user")
    return Response(status_code=204)


@router.get("/me", response_model=UserOut)
async def read_me(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> UserOut:
    return await build_user(session, user)


@router.patch("/me", response_model=UserOut)
async def patch_me(
    body: MePatch,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user.pet_type = body.pet_type
    return await build_user(session, user)


@router.delete("/me", status_code=204)
async def remove_me(
    user: User = Depends(get_user_for_deletion),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
) -> Response:
    await delete_account(session, storage, user)
    return Response(status_code=204)


@router.get("/credits", response_model=CreditOut)
async def credits(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> CreditOut:
    profile = await build_user(session, user)
    return profile.credits


@router.get("/templates", response_model=TemplateListOut)
async def templates(
    page: int = Query(default=1, ge=1),
    category: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
) -> TemplateListOut:
    _check_category(category)
    rows, total, featured_id = await list_public_templates(session, page=page, category=category)
    items = [await build_template(storage, row) for row in rows]
    return TemplateListOut(items=items, page=page, page_size=PAGE_SIZE, total=total, featured_id=featured_id)


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def template_detail(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
) -> TemplateOut:
    template = await get_published_template(session, template_id)
    return await build_template(storage, template)


@router.post("/uploads/image", response_model=ImageUploadOut)
async def upload_image(
    body: ImageUploadIn,
    user: User = Depends(get_current_user),
    storage: Storage = Depends(get_storage_dep),
) -> ImageUploadOut:
    if body.content_length > MAX_IMAGE_BYTES:
        raise AppError(400, "IMAGE_INVALID", "图片需为 9:16 的 JPEG，长边不超过 1280，大小不超过 1.5MB")
    image_key = f"staging/{user.id}/{new_id()}.jpg"
    upload_url = await storage.presign_put(image_key, body.content_type, body.content_length, expires=600)
    return ImageUploadOut(
        image_key=image_key,
        upload_url=upload_url,
        headers={"Content-Type": body.content_type},
        expires_in=600,
    )


@router.post("/generations", response_model=GenerationOut, status_code=201)
async def create_task(
    body: GenerationIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> GenerationOut:
    task = await create_generation(
        session,
        storage,
        user_id=user.id,
        template_id=body.template_id,
        image_key=body.image_key,
        ownership_confirmed=body.ownership_confirmed,
        idempotency_key=parse_idempotency_key(idempotency_key),
    )
    template = await session.get(Template, task.template_id)
    if template is None:
        raise AppError(404, "NOT_FOUND", "模板不存在")
    mapping = await media_map(session, [task])
    return await build_generation(storage, task, template, await media_for(task, mapping))


@router.get("/generations", response_model=GenerationListOut)
async def generations(
    page: int = Query(default=1, ge=1),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
) -> GenerationListOut:
    rows, total = await list_generations(session, user.id, page)
    tasks = [task for task, _template in rows]
    mapping = await media_map(session, tasks)
    items = []
    for task, template in rows:
        items.append(await build_generation(storage, task, template, await media_for(task, mapping)))
    return GenerationListOut(items=items, page=page, page_size=PAGE_SIZE, total=total)


@router.get("/generations/{task_id}", response_model=GenerationOut)
async def generation_detail(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
) -> GenerationOut:
    task, template = await load_generation(session, user.id, task_id)
    mapping = await media_map(session, [task])
    return await build_generation(storage, task, template, await media_for(task, mapping))


@router.delete("/generations/{task_id}", status_code=204)
async def remove_generation(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
) -> Response:
    await delete_generation(session, storage, user.id, task_id)
    return Response(status_code=204)


def _check_category(category: str | None) -> None:
    if category is None:
        return
    if category not in {"recommend", "dance", "comedy", "daily"}:
        raise AppError(422, "VALIDATION", "分类不正确")
