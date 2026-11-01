from urllib.parse import urlparse

from app.main import app
from app.samples import make_jpeg, tiny_mp4

CONSENT = {"terms_version": "2026-10-28", "privacy_version": "2026-10-28"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def signed_request(client, method: str, url: str, content: bytes | None = None, headers: dict | None = None):
    parsed = urlparse(url)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return await client.request(method, path, content=content, headers=headers)


async def login(client, phone: str = "13800138000", pet_type: str | None = None, code: str = "246810"):
    sms = await client.post("/v1/auth/sms", json={"phone": phone})
    assert sms.status_code == 200, sms.text
    body = {"phone": phone, "code": code, "consent": CONSENT}
    if pet_type is not None:
        body["pet_type"] = pet_type
    return await client.post("/v1/auth/login", json=body)


async def admin_headers(client) -> dict[str, str]:
    from app.core.db import get_sessionmaker
    from app.seed import ensure_admin

    async with get_sessionmaker()() as session:
        await ensure_admin(session)
        await session.commit()
    response = await client.post(
        "/admin-api/v1/auth/login",
        json={"username": "admin", "password": "petdance-admin"},
    )
    assert response.status_code == 200, response.text
    return bearer(response.json()["access_token"])


async def publish_template(client, headers, **overrides) -> dict:
    cover = make_jpeg()
    preview = tiny_mp4()
    cover_up = await client.post(
        "/admin-api/v1/uploads",
        headers=headers,
        json={"kind": "cover", "content_type": "image/jpeg", "content_length": len(cover)},
    )
    assert cover_up.status_code == 200, cover_up.text
    preview_up = await client.post(
        "/admin-api/v1/uploads",
        headers=headers,
        json={"kind": "preview", "content_type": "video/mp4", "content_length": len(preview)},
    )
    assert preview_up.status_code == 200, preview_up.text
    cover_body = cover_up.json()
    preview_body = preview_up.json()
    stored = await signed_request(client, "PUT", cover_body["upload_url"], cover, cover_body["headers"])
    assert stored.status_code == 200, stored.text
    stored = await signed_request(client, "PUT", preview_body["upload_url"], preview, preview_body["headers"])
    assert stored.status_code == 200, stored.text
    payload = {
        "title": "测试模板",
        "subtitle": "一只宠物",
        "category": "dance",
        "pet_scope": "any",
        "prompt": "PROMPT_SECRET_请勿下发",
        "negative_prompt": "不要文字",
        "credit_cost": 10,
        "cover_key": cover_body["key"],
        "preview_key": preview_body["key"],
        "status": "published",
    }
    payload.update(overrides)
    created = await client.post("/admin-api/v1/templates", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    return created.json()


async def upload_pet(client, headers, data: bytes | None = None) -> str:
    image = data if data is not None else make_jpeg()
    response = await client.post(
        "/v1/uploads/image",
        headers=headers,
        json={"content_type": "image/jpeg", "content_length": len(image)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    stored = await signed_request(client, "PUT", payload["upload_url"], image, payload["headers"])
    assert stored.status_code == 200, stored.text
    return payload["image_key"]


async def clear_sms_cooldown(phone: str) -> None:
    await app.state.redis.delete(f"sms:cool:{phone}")
