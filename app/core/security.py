import hashlib
import secrets
import uuid
from datetime import timedelta

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.core.clock import utcnow
from app.core.config import get_settings

_DUMMY_HASH: str | None = None


def hash_password(password: str) -> str:
    rounds = 4 if get_settings().env == "test" else 12
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()


def dummy_password_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-not-used")
    return _DUMMY_HASH


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def encode_access_token(*, subject_id: uuid.UUID, audience: str, role: str | None = None) -> tuple[str, int]:
    settings = get_settings()
    ttl = settings.app_access_ttl_sec if audience == "app" else settings.admin_access_ttl_sec
    secret = settings.app_jwt_secret if audience == "app" else settings.admin_jwt_secret
    now = utcnow()
    payload: dict = {
        "sub": str(subject_id),
        "aud": audience,
        "iss": "pet_dance",
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
        "typ": "access",
    }
    if role:
        payload["role"] = role
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token, ttl


def decode_access_token(token: str, audience: str) -> dict:
    settings = get_settings()
    secret = settings.app_jwt_secret if audience == "app" else settings.admin_jwt_secret
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=audience,
            issuer="pet_dance",
            leeway=10,
        )
    except InvalidTokenError as exc:
        raise ValueError("invalid token") from exc
    if payload.get("typ") != "access":
        raise ValueError("invalid token")
    return payload


def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return raw, digest


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
