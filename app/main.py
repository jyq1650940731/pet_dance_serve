import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.admin import router as admin_router
from app.api.deps import get_session
from app.api.local_storage import router as local_router
from app.api.v1 import router as v1_router
from app.core.config import get_settings
from app.core.db import Base, get_engine, get_sessionmaker
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.domain.phones import mask_text
from app.services.common import get_settings_row
from app.storage import build_storage, set_storage

logger = logging.getLogger(__name__)


async def build_redis():
    settings = get_settings()
    if settings.redis_url == "memory":
        import fakeredis.aioredis

        return fakeredis.aioredis.FakeRedis(decode_responses=True)
    import redis.asyncio as aioredis

    return aioredis.from_url(settings.redis_url, decode_responses=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        async with get_engine().begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    storage = build_storage()
    set_storage(storage)
    app.state.storage = storage
    app.state.redis = await build_redis()
    try:
        async with get_sessionmaker()() as session:
            await get_settings_row(session)
            await session.commit()
    except Exception:
        if settings.env != "development":
            raise
        logger.exception("数据库尚未就绪，跳过初始化配置")
    yield
    redis = app.state.redis
    close = getattr(redis, "aclose", None) or getattr(redis, "close", None)
    if close is not None:
        result = close()
        if hasattr(result, "__await__"):
            await result
    if settings.env != "test":
        await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="pet_dance",
        version="0.4.0",
        summary="第 1–4 周服务端",
        description=(
            "账号、模板、任务状态机、冻结退点、对象存储，以及员工登录和模板草稿。"
            "成片暂时复制模板预览片，不调用图生视频。支付、审核、可灵和运营概览不在这一阶段。"
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(v1_router)
    app.include_router(admin_router)
    if settings.storage_backend == "local":
        app.include_router(local_router)
    _install_handlers(app)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "version": "0.4.0"}

    @app.get("/health/ready")
    async def ready(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
        await session.execute(text("SELECT 1"))
        await request.app.state.redis.ping()
        return {"status": "ok"}

    return app


def _install_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        payload = {"code": exc.code, "message": exc.message, **exc.extra}
        return JSONResponse(status_code=exc.status_code, content={"error": payload})

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": "HTTP_ERROR", "message": message}})

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(part) for part in error.get("loc", [])], "msg": error.get("msg"), "type": error.get("type")}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION", "message": "请求参数不正确", "details": details}},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常")
        if get_settings().env == "production":
            message = "服务暂时不可用"
        else:
            message = mask_text(f"{type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL", "message": message}})


app = create_app()
