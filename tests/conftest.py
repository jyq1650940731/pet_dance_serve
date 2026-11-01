import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["REDIS_URL"] = "memory"
os.environ["QUEUE_MODE"] = "off"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["STORAGE_LOCAL_DIR"] = str(ROOT / "var" / "test-storage")
os.environ["STORAGE_PUBLIC_BASE"] = "http://testserver"
os.environ["SMS_PROVIDER"] = "dev"
os.environ["SMS_DEV_CODE"] = "246810"
os.environ["APP_JWT_SECRET"] = "test-app-jwt-secret-32bytes-minimum"
os.environ["ADMIN_JWT_SECRET"] = "test-admin-jwt-secret-32bytes-min"
os.environ["ADMIN_PASSWORD"] = "petdance-admin"
os.environ["TERMS_VERSION"] = "2026-10-28"
os.environ["PRIVACY_VERSION"] = "2026-10-28"

from app.core.config import get_settings

get_settings.cache_clear()

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.db import Base, get_engine
from app.main import app


@pytest.fixture
async def client():
    path = Path(get_settings().storage_local_dir)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
