import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.core.clock import CN
from app.core.config import get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def send_code(redis, phone: str) -> int:
    settings = get_settings()
    cool_key = f"sms:cool:{phone}"
    if await redis.exists(cool_key):
        ttl = await redis.ttl(cool_key)
        raise AppError(429, "SMS_COOLDOWN", "请稍后再获取验证码", extra={"retry_after": max(int(ttl), 1)})

    day = datetime.now(CN).strftime("%Y%m%d")
    day_key = f"sms:day:{phone}:{day}"
    sent = int(await redis.get(day_key) or 0)
    if sent >= settings.sms_daily_limit:
        raise AppError(429, "SMS_DAILY_LIMIT", "今日验证码次数已用完")

    code = settings.sms_dev_code if settings.sms_provider == "dev" else f"{secrets_code()}"
    if settings.sms_provider == "aliyun":
        await _send_aliyun(phone, code)
    elif settings.sms_provider != "dev":
        raise AppError(503, "SMS_UNAVAILABLE", "短信服务未配置")

    await redis.set(f"sms:code:{phone}", _digest(code), ex=settings.sms_code_ttl_sec)
    await redis.set(cool_key, "1", ex=settings.sms_cooldown_sec)
    count = await redis.incr(day_key)
    if int(count) == 1:
        await redis.expire(day_key, 48 * 3600)
    await redis.delete(f"sms:try:{phone}")
    logger.info("验证码已发送")
    return settings.sms_cooldown_sec


def secrets_code() -> str:
    import secrets

    return f"{secrets.randbelow(1_000_000):06d}"


async def verify_code(redis, phone: str, code: str) -> None:
    key = f"sms:code:{phone}"
    stored = await redis.get(key)
    if not stored:
        raise AppError(400, "SMS_INVALID", "验证码错误或已过期")
    if isinstance(stored, bytes):
        stored = stored.decode()
    tries_key = f"sms:try:{phone}"
    if not hmac.compare_digest(stored, _digest(code)):
        tries = int(await redis.incr(tries_key))
        await redis.expire(tries_key, 300)
        if tries >= 5:
            await redis.delete(key)
        raise AppError(400, "SMS_INVALID", "验证码错误或已过期")
    await redis.delete(key)
    await redis.delete(tries_key)


def _percent(value: str) -> str:
    return quote(value, safe="~")


async def _send_aliyun(phone: str, code: str) -> None:
    settings = get_settings()
    if not settings.aliyun_sms_access_key_id or not settings.aliyun_sms_access_key_secret:
        raise AppError(503, "SMS_UNAVAILABLE", "短信服务未配置")
    if not settings.aliyun_sms_sign_name or not settings.aliyun_sms_template_code:
        raise AppError(503, "SMS_UNAVAILABLE", "短信服务未配置")
    params = {
        "AccessKeyId": settings.aliyun_sms_access_key_id,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": settings.aliyun_sms_sign_name,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "TemplateCode": settings.aliyun_sms_template_code,
        "TemplateParam": json.dumps({"code": code}, separators=(",", ":")),
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
    }
    canonical = "&".join(f"{_percent(key)}={_percent(params[key])}" for key in sorted(params))
    string_to_sign = "GET&%2F&" + _percent(canonical)
    secret = (settings.aliyun_sms_access_key_secret + "&").encode()
    signature = base64.b64encode(hmac.new(secret, string_to_sign.encode(), hashlib.sha1).digest()).decode()
    params["Signature"] = signature
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get("https://dysmsapi.aliyuncs.com/", params=params)
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("短信服务返回无法解析")
        raise AppError(502, "SMS_UNAVAILABLE", "验证码发送失败") from exc
    if payload.get("Code") != "OK":
        logger.warning("短信服务拒绝 code=%s", payload.get("Code"))
        raise AppError(502, "SMS_UNAVAILABLE", "验证码发送失败")
