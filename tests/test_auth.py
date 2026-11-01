from app.core.config import get_settings
from app.main import app
from tests.helpers import CONSENT, clear_sms_cooldown, login


async def test_sms_login_gift_profile_and_refresh(client):
    denied = await client.get("/v1/me")
    assert denied.status_code == 401

    response = await login(client, pet_type="cat")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gifted"] is True
    assert body["expires_in"] == 900
    assert body["user"]["pet_type"] == "cat"
    assert body["user"]["phone_masked"] == "138****8000"
    assert body["user"]["credits"] == {"balance": 20, "frozen": 0}
    assert "13800138000" not in response.text

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = await client.get("/v1/me", headers=headers)
    assert me.status_code == 200
    patched = await client.patch("/v1/me", headers=headers, json={"pet_type": "dog"})
    assert patched.json()["pet_type"] == "dog"

    refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    await client.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})
    again = await client.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert again.status_code == 401


async def test_consent_is_required_before_the_code_is_consumed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "terms_version", "2099-01-01")
    sms = await client.post("/v1/auth/sms", json={"phone": "13800138001"})
    assert sms.status_code == 200
    rejected = await client.post(
        "/v1/auth/login",
        json={"phone": "13800138001", "code": "246810", "consent": CONSENT},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "CONSENT_OUTDATED"
    accepted = await client.post(
        "/v1/auth/login",
        json={
            "phone": "13800138001",
            "code": "246810",
            "consent": {"terms_version": "2099-01-01", "privacy_version": "2026-10-28"},
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["gifted"] is True


async def test_wrong_code_and_daily_limit(client, monkeypatch):
    phone = "13800138002"
    await client.post("/v1/auth/sms", json={"phone": phone})
    for _ in range(5):
        bad = await client.post(
            "/v1/auth/login",
            json={"phone": phone, "code": "000000", "consent": CONSENT},
        )
        assert bad.status_code == 400
    burned = await client.post(
        "/v1/auth/login",
        json={"phone": phone, "code": "246810", "consent": CONSENT},
    )
    assert burned.status_code == 400

    await clear_sms_cooldown(phone)
    monkeypatch.setattr(get_settings(), "sms_daily_limit", 1)
    limited = await client.post("/v1/auth/sms", json={"phone": phone})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "SMS_DAILY_LIMIT"


async def test_resend_is_cooled_down(client):
    phone = "13800138003"
    first = await client.post("/v1/auth/sms", json={"phone": phone})
    assert first.status_code == 200
    second = await client.post("/v1/auth/sms", json={"phone": phone})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "SMS_COOLDOWN"


async def test_delete_account_does_not_gift_again(client):
    phone = "13800138004"
    first = await login(client, phone)
    assert first.status_code == 200, first.text
    token = first.json()["access_token"]
    user_id = first.json()["user"]["id"]
    removed = await client.delete("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert removed.status_code == 204
    stale = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert stale.status_code == 401
    await clear_sms_cooldown(phone)
    second = await login(client, phone)
    assert second.status_code == 200, second.text
    assert second.json()["gifted"] is False
    assert second.json()["user"]["credits"]["balance"] == 0
    assert second.json()["user"]["id"] != user_id


async def test_banned_user_cannot_login(client):
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models import User

    phone = "13800138005"
    first = await login(client, phone)
    assert first.status_code == 200
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.phone == phone))
        user.status = "banned"
        await session.commit()
    await clear_sms_cooldown(phone)
    second = await login(client, phone)
    assert second.status_code == 403
    assert second.json()["error"]["code"] == "ACCOUNT_BANNED"


async def test_meta_reports_agreement_and_gift(client):
    meta = await client.get("/v1/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["maintenance"] is False
    assert body["gift_credits"] == 20
    assert body["terms_version"] == "2026-10-28"
    assert body["cheapest_credit_cost"] is None


async def test_app_token_cannot_call_admin(client):
    logged = await login(client, "13800138006")
    response = await client.get(
        "/admin-api/v1/templates",
        headers={"Authorization": f"Bearer {logged.json()['access_token']}"},
    )
    assert response.status_code == 401
