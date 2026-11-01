import asyncio
import logging
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import Base, get_engine, get_sessionmaker
from app.core.logging import setup_logging
from app.core.security import hash_password
from app.models import StaffUser, Template
from app.samples import make_jpeg, tiny_mp4
from app.services.common import get_settings_row
from app.storage import build_storage, set_storage

logger = logging.getLogger(__name__)

NAMESPACE = uuid.UUID("6f0c1a2e-7b4d-4e1a-9c3d-8a1b2c3d4e5f")
SUFFIX = "保持参考图中同一只宠物的毛色、花纹、耳朵和体型。"
NEGATIVE = "多余肢体、第二只动物、文字水印、人物正脸、镜头切换"

CATALOG = [
    ("客厅蹦迪", "宠物在客厅地板上随节拍点头、抬爪", "dance", "any", "一只宠物在客厅木地板上随着节拍点头，并抬起一只前爪，单一机位，固定镜头，五秒内只做这一件事。", (232, 176, 138)),
    ("窗边慢摇", "靠窗坐下，轻轻摆动", "dance", "cat", "一只宠物靠窗坐下，身体轻轻左右摆动，单一机位，固定镜头，五秒内只做这一件事。", (176, 196, 176)),
    ("被抓包", "从零食袋旁抬头，表情停顿", "comedy", "any", "一只宠物从零食袋旁抬头看向镜头，表情停顿，单一机位，固定镜头，五秒内只做这一件事。", (230, 196, 150)),
    ("假装睡觉", "趴着，眼睛睁一条缝", "comedy", "cat", "一只宠物趴着假装睡觉，眼睛睁开一条缝，单一机位，固定镜头，五秒内只做这一件事。", (206, 176, 140)),
    ("出门回头", "走向门口，回头看镜头", "daily", "dog", "一只宠物走向门口，停下并回头看镜头，单一机位，固定镜头，五秒内只做这一件事。", (168, 188, 176)),
    ("沙发霸主", "趴在沙发扶手上，镜头轻推", "daily", "cat", "一只宠物趴在沙发扶手上，镜头缓慢轻推，单一机位，五秒内只做这一件事。", (214, 180, 156)),
    ("阳光打盹", "光斑里缓慢眨眼", "daily", "cat", "一只宠物躺在光斑里缓慢眨眼，单一机位，固定镜头，五秒内只做这一件事。", (196, 204, 156)),
    ("小跑过来", "从房间另一头跑向镜头并停下", "daily", "dog", "一只宠物从房间另一头跑向镜头并停下，单一机位，五秒内只做这一件事。", (220, 186, 146)),
]


def template_id(title: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, title)


async def ensure_admin(session) -> StaffUser:
    settings = get_settings()
    existing = await session.scalar(select(StaffUser).where(StaffUser.username == settings.admin_username))
    if existing is not None:
        return existing
    if len(settings.admin_password) < 10:
        raise RuntimeError("ADMIN_PASSWORD 至少 10 位")
    staff = StaffUser(
        id=uuid.uuid4(),
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        role="admin",
        status="active",
    )
    session.add(staff)
    await session.flush()
    logger.info("已创建管理员 %s，密码来自环境变量 ADMIN_PASSWORD", staff.username)
    return staff


async def seed_templates(session, storage) -> None:
    preview = tiny_mp4()
    for index, (title, subtitle, category, pet_scope, prompt, color) in enumerate(CATALOG, start=1):
        item_id = template_id(title)
        cover_key = f"templates/covers/{item_id}.jpg"
        preview_key = f"templates/previews/{item_id}.mp4"
        if await storage.head(cover_key) is None:
            await storage.put_bytes(cover_key, make_jpeg(color=color), "image/jpeg")
        if await storage.head(preview_key) is None:
            await storage.put_bytes(preview_key, preview, "video/mp4")
        existing = await session.get(Template, item_id)
        if existing is not None:
            continue
        session.add(
            Template(
                id=item_id,
                title=title,
                subtitle=subtitle,
                category=category,
                status="published",
                sort=index * 10,
                duration_sec=5,
                aspect_ratio="9:16",
                credit_cost=10,
                pet_scope=pet_scope,
                provider="kling",
                model_name="kling-v2.5-turbo",
                prompt=prompt + SUFFIX,
                negative_prompt=NEGATIVE,
                cover_key=cover_key,
                preview_key=preview_key,
            )
        )
    await session.flush()


async def main() -> None:
    setup_logging()
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        async with get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    storage = build_storage()
    set_storage(storage)
    async with get_sessionmaker()() as session:
        await get_settings_row(session)
        await ensure_admin(session)
        await seed_templates(session, storage)
        await session.commit()
    logger.info("种子数据已就绪")


if __name__ == "__main__":
    asyncio.run(main())
