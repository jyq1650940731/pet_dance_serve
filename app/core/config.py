import re
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "development"
    database_url: str = "postgresql+psycopg://pet:pet@localhost:5432/pet_dance"
    redis_url: str = "redis://localhost:6379/0"
    queue_mode: str = "arq"
    storage_backend: str = "local"
    storage_local_dir: str = "./var/storage"
    storage_public_base: str = "http://localhost:8080"
    storage_secret: str = ""

    app_jwt_secret: str = "dev-app-jwt-secret-change-me-32b"
    admin_jwt_secret: str = "dev-admin-jwt-secret-change-me-32"
    app_access_ttl_sec: int = 15 * 60
    app_refresh_ttl_sec: int = 30 * 24 * 3600
    admin_access_ttl_sec: int = 2 * 3600
    admin_refresh_ttl_sec: int = 8 * 3600

    sms_provider: str = "dev"
    sms_dev_code: str = "246810"
    sms_code_ttl_sec: int = 300
    sms_cooldown_sec: int = 60
    sms_daily_limit: int = 10
    aliyun_sms_access_key_id: str = ""
    aliyun_sms_access_key_secret: str = ""
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""

    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_cdn_base: str = ""

    admin_username: str = "admin"
    admin_password: str = "petdance-admin"

    terms_version: str = "2026-10-28"
    privacy_version: str = "2026-10-28"
    placeholder_step_seconds: float = 0
    generation_timeout_sec: int = 8 * 60
    works_retention: int = 200
    cors_origins: str = "http://localhost:5173"

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.queue_mode not in {"arq", "inline", "off"}:
            raise ValueError("QUEUE_MODE 只能是 arq、inline 或 off")
        if self.storage_backend not in {"local", "oss"}:
            raise ValueError("STORAGE_BACKEND 只能是 local 或 oss")
        if self.sms_provider == "dev" and not re.fullmatch(r"\d{6}", self.sms_dev_code):
            raise ValueError("SMS_DEV_CODE 必须是 6 位数字")
        if self.env == "production":
            self._check_production()
        return self

    def _check_production(self) -> None:
        problems: list[str] = []
        if self.sms_provider == "dev":
            problems.append("生产环境不能使用短信开发模式")
        if self.app_jwt_secret.startswith("dev-") or len(self.app_jwt_secret) < 32:
            problems.append("APP_JWT_SECRET")
        if self.admin_jwt_secret.startswith("dev-") or len(self.admin_jwt_secret) < 32:
            problems.append("ADMIN_JWT_SECRET")
        if self.app_jwt_secret == self.admin_jwt_secret:
            problems.append("App 与后台令牌密钥必须不同")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL")
        if self.redis_url in {"memory", "fakeredis"}:
            problems.append("REDIS_URL")
        if self.queue_mode != "arq":
            problems.append("QUEUE_MODE")
        if self.storage_backend != "oss":
            problems.append("STORAGE_BACKEND")
        if self.admin_password == "petdance-admin":
            problems.append("ADMIN_PASSWORD")
        if not self.oss_endpoint or not self.oss_bucket:
            problems.append("OSS")
        if problems:
            raise ValueError("生产配置不完整：" + "、".join(problems))

    def signing_secret(self) -> str:
        return self.storage_secret or self.app_jwt_secret

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
