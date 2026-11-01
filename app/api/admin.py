import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_staff, get_session, get_storage_dep
from app.core.errors import AppError
from app.domain.constants import PAGE_SIZE
from app.models import StaffUser
from app.present import build_admin_template
from app.schemas import (
    AdminTemplateListOut,
    AdminTemplateOut,
    AdminUploadIn,
    AdminUploadOut,
    LogoutIn,
    RefreshIn,
    RefreshOut,
    StaffLoginIn,
    StaffTokenOut,
    TemplatePatch,
    TemplateWrite,
)
from app.services.accounts import authenticate_staff, issue_session, refresh_session, revoke_refresh_token
from app.services.catalog import (
    create_template,
    duplicate_template,
    get_template,
    list_admin_templates,
    today_count,
    update_template,
)
from app.services.common import new_id
from app.storage import Storage

router = APIRouter(prefix="/admin-api/v1")

UPLOADS = {
    "cover": {
        "types": {"image/jpeg", "image/png", "image/webp"},
        "max": 5 * 1024 * 1024,
        "prefix": "templates/covers",
        "ext": {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"},
    },
    "preview": {
        "types": {"video/mp4"},
        "max": 30 * 1024 * 1024,
        "prefix": "templates/previews",
        "ext": {"video/mp4": ".mp4"},
    },
    "audio": {
        "types": {"audio/mpeg", "audio/aac", "audio/mp4"},
        "max": 15 * 1024 * 1024,
        "prefix": "templates/audios",
        "ext": {"audio/mpeg": ".mp3", "audio/aac": ".aac", "audio/mp4": ".m4a"},
    },
}


@router.post("/auth/login", response_model=StaffTokenOut)
async def login(body: StaffLoginIn, session: AsyncSession = Depends(get_session)) -> StaffTokenOut:
    staff = await authenticate_staff(session, body.username, body.password)
    access, refresh, expires_in = await issue_session(session, subject_type="staff", subject_id=staff.id, role=staff.role)
    return StaffTokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        role=staff.role,
        username=staff.username,
    )


@router.post("/auth/refresh", response_model=RefreshOut)
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)) -> RefreshOut:
    access, refresh_token, expires_in = await refresh_session(session, body.refresh_token, "staff")
    return RefreshOut(access_token=access, refresh_token=refresh_token, expires_in=expires_in)


@router.post("/auth/logout", status_code=204)
async def logout(body: LogoutIn, session: AsyncSession = Depends(get_session)) -> Response:
    await revoke_refresh_token(session, body.refresh_token, "staff")
    return Response(status_code=204)


@router.get("/templates", response_model=AdminTemplateListOut)
async def templates(
    page: int = Query(default=1, ge=1),
    category: str | None = None,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
    _staff: StaffUser = Depends(get_current_staff),
) -> AdminTemplateListOut:
    if category is not None and category not in {"recommend", "dance", "comedy", "daily"}:
        raise AppError(422, "VALIDATION", "分类不正确")
    if status is not None and status not in {"draft", "published", "unpublished"}:
        raise AppError(422, "VALIDATION", "状态不正确")
    rows, total, counts = await list_admin_templates(session, page=page, category=category, status=status)
    items = [await build_admin_template(storage, row, counts.get(row.id, 0)) for row in rows]
    return AdminTemplateListOut(items=items, page=page, page_size=PAGE_SIZE, total=total)


@router.post("/templates", response_model=AdminTemplateOut, status_code=201)
async def create(
    body: TemplateWrite,
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
    staff: StaffUser = Depends(get_current_staff),
) -> AdminTemplateOut:
    template = await create_template(session, storage, staff, body.model_dump())
    return await build_admin_template(storage, template, 0)


@router.get("/templates/{template_id}", response_model=AdminTemplateOut)
async def detail(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
    _staff: StaffUser = Depends(get_current_staff),
) -> AdminTemplateOut:
    template = await get_template(session, template_id)
    return await build_admin_template(storage, template, await today_count(session, template.id))


@router.patch("/templates/{template_id}", response_model=AdminTemplateOut)
async def patch(
    template_id: uuid.UUID,
    body: TemplatePatch,
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
    staff: StaffUser = Depends(get_current_staff),
) -> AdminTemplateOut:
    template = await get_template(session, template_id)
    updated = await update_template(session, storage, staff, template, body.model_dump(exclude_unset=True))
    return await build_admin_template(storage, updated, await today_count(session, updated.id))


@router.post("/templates/{template_id}/duplicate", response_model=AdminTemplateOut, status_code=201)
async def duplicate(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    storage: Storage = Depends(get_storage_dep),
    staff: StaffUser = Depends(get_current_staff),
) -> AdminTemplateOut:
    source = await get_template(session, template_id)
    copy = await duplicate_template(session, storage, staff, source)
    return await build_admin_template(storage, copy, 0)


@router.post("/uploads", response_model=AdminUploadOut)
async def upload(
    body: AdminUploadIn,
    storage: Storage = Depends(get_storage_dep),
    _staff: StaffUser = Depends(get_current_staff),
) -> AdminUploadOut:
    spec = UPLOADS[body.kind]
    content_type = body.content_type.split(";", 1)[0].strip().lower()
    if content_type not in spec["types"]:
        raise AppError(400, "UPLOAD_INVALID", "文件类型不正确")
    if body.content_length > spec["max"]:
        raise AppError(400, "UPLOAD_TOO_LARGE", "文件超过大小限制")
    key = f"{spec['prefix']}/{new_id()}{spec['ext'][content_type]}"
    upload_url = await storage.presign_put(key, content_type, body.content_length, expires=600)
    return AdminUploadOut(key=key, upload_url=upload_url, headers={"Content-Type": content_type}, expires_in=600)
