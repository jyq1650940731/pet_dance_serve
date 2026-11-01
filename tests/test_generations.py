import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.jobs import advance_generation
from app.models import AppSetting, CreditAccount, CreditLedger, GenerationTask, Media
from app.samples import make_jpeg
from app.services.generations import step_once
from tests.helpers import admin_headers, bearer, login, publish_template, signed_request, upload_pet


async def _user(client, phone="13700137000"):
    response = await login(client, phone)
    assert response.status_code == 200, response.text
    body = response.json()
    return body, bearer(body["access_token"])


async def test_generate_freezes_then_consumes_the_preview(client):
    headers = await admin_headers(client)
    template = await publish_template(client, headers)
    user, auth = await _user(client)
    image_key = await upload_pet(client, auth)
    created = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": template["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "queued"
    assert created.json()["stage_label"] == "排队中"
    assert "PROMPT_SECRET" not in created.text

    task_id = uuid.UUID(created.json()["id"])
    assert await step_once(task_id) == "moderating"
    assert await advance_generation({"redis": None}, created.json()["id"]) == "succeeded"

    detail = await client.get(f"/v1/generations/{created.json()['id']}", headers=auth)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["playable"] is True
    assert body["stage_label"] == "可播放"
    assert body["template_title"] == template["title"]
    video = await signed_request(client, "GET", body["video_url"])
    assert video.status_code == 200
    assert video.content[4:8] == b"ftyp"

    credits = await client.get("/v1/credits", headers=auth)
    assert credits.json() == {"balance": 10, "frozen": 0}
    async with get_sessionmaker()() as session:
        task = await session.get(GenerationTask, task_id)
        ledgers = (
            await session.execute(
                select(CreditLedger)
                .where(CreditLedger.user_id == uuid.UUID(user["user"]["id"]))
                .order_by(CreditLedger.created_at.asc())
            )
        ).scalars().all()
    assert task.moderating_at and task.generating_at and task.muxing_at and task.finished_at
    assert task.attempt_count == 1
    assert task.provider_task_id is None
    assert [row.entry_type for row in ledgers] == ["gift", "freeze", "consume"]
    assert user["user"]["credits"]["balance"] == 20


async def test_delete_in_progress_refunds_once(client):
    headers = await admin_headers(client)
    template = await publish_template(client, headers, title="退点模板")
    _user_body, auth = await _user(client, "13700137001")
    image_key = await upload_pet(client, auth)
    created = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": template["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    assert created.status_code == 201, created.text
    removed = await client.delete(f"/v1/generations/{created.json()['id']}", headers=auth)
    assert removed.status_code == 204
    assert (await client.get("/v1/credits", headers=auth)).json() == {"balance": 20, "frozen": 0}
    missing = await client.get(f"/v1/generations/{created.json()['id']}", headers=auth)
    assert missing.status_code == 404
    assert await step_once(uuid.UUID(created.json()["id"])) == "failed"
    assert (await client.get("/v1/credits", headers=auth)).json()["balance"] == 20


async def test_timeout_refunds_and_is_idempotent(client):
    headers = await admin_headers(client)
    template = await publish_template(client, headers, title="超时模板")
    _user_body, auth = await _user(client, "13700137002")
    image_key = await upload_pet(client, auth)
    created = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": template["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    task_id = uuid.UUID(created.json()["id"])
    async with get_sessionmaker()() as session:
        task = await session.get(GenerationTask, task_id)
        task.queued_at = utcnow() - timedelta(minutes=9)
        await session.commit()
    assert await step_once(task_id) == "failed"
    detail = await client.get(f"/v1/generations/{task_id}", headers=auth)
    assert detail.json()["error_code"] == "TIMEOUT"
    assert detail.json()["error_message"] == "生成超时，点数已退回"
    assert detail.json()["playable"] is False
    assert await step_once(task_id) == "failed"
    assert (await client.get("/v1/credits", headers=auth)).json() == {"balance": 20, "frozen": 0}


async def test_guards_around_create(client):
    headers = await admin_headers(client)
    cheap = await publish_template(client, headers, title="便宜", credit_cost=10)
    expensive = await publish_template(client, headers, title="贵", credit_cost=100, sort=2)
    _user_body, auth = await _user(client, "13700137003")
    image_key = await upload_pet(client, auth)

    denied = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": cheap["id"], "image_key": image_key, "ownership_confirmed": False},
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "OWNERSHIP_REQUIRED"

    poor = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": expensive["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    assert poor.status_code == 409
    assert poor.json()["error"]["code"] == "INSUFFICIENT_CREDITS"

    tiny = await upload_pet(client, auth, make_jpeg(100, 100))
    invalid = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": cheap["id"], "image_key": tiny, "ownership_confirmed": True},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "IMAGE_INVALID"
    wide = await upload_pet(client, auth, make_jpeg(1280, 720))
    wide_result = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": cheap["id"], "image_key": wide, "ownership_confirmed": True},
    )
    assert wide_result.status_code == 400

    other = await login(client, "13700137004")
    stolen = await client.post(
        "/v1/generations",
        headers=bearer(other.json()["access_token"]),
        json={"template_id": cheap["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    assert stolen.status_code == 400

    offline = await client.patch(f"/admin-api/v1/templates/{cheap['id']}", headers=headers, json={"status": "unpublished"})
    assert offline.status_code == 200
    gone = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": cheap["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    assert gone.status_code == 409
    assert (await client.get("/v1/credits", headers=auth)).json() == {"balance": 20, "frozen": 0}


async def test_idempotency_maintenance_and_list_order(client, monkeypatch):
    headers = await admin_headers(client)
    template = await publish_template(client, headers, title="队列模板", sort=3)
    _user_body, auth = await _user(client, "13700137005")
    image_key = await upload_pet(client, auth)
    payload = {"template_id": template["id"], "image_key": image_key, "ownership_confirmed": True}
    idem = {"Idempotency-Key": "task-001"}
    first = await client.post("/v1/generations", headers={**auth, **idem}, json=payload)
    second = await client.post("/v1/generations", headers={**auth, **idem}, json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert (await client.get("/v1/credits", headers=auth)).json() == {"balance": 10, "frozen": 10}

    async with get_sessionmaker()() as session:
        row = await session.get(AppSetting, 1)
        row.maintenance = True
        row.maintenance_message = "今晚维护"
        await session.commit()
    meta = await client.get("/v1/meta")
    assert meta.json()["maintenance"] is True
    assert meta.json()["maintenance_message"] == "今晚维护"
    another = await upload_pet(client, auth)
    blocked = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": template["id"], "image_key": another, "ownership_confirmed": True},
    )
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "MAINTENANCE"

    async with get_sessionmaker()() as session:
        row = await session.get(AppSetting, 1)
        row.maintenance = False
        await session.commit()
    finished = await advance_generation({"redis": None}, first.json()["id"])
    assert finished == "succeeded"
    image_key = await upload_pet(client, auth)
    newer = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": template["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    listed = await client.get("/v1/generations", headers=auth)
    assert listed.status_code == 200
    assert [item["status"] for item in listed.json()["items"]] == ["queued", "succeeded"]
    assert listed.json()["items"][0]["id"] == newer.json()["id"]

    monkeypatch.setattr(get_settings(), "works_retention", 1)
    await advance_generation({"redis": None}, newer.json()["id"])
    kept = await client.get("/v1/generations", headers=auth)
    statuses = {item["id"]: item for item in kept.json()["items"]}
    assert statuses[newer.json()["id"]]["playable"] is True
    assert statuses[first.json()["id"]]["playable"] is False
    assert statuses[first.json()["id"]]["error_message"] == "该作品已清理"


async def test_taken_down_result_cannot_play(client):
    headers = await admin_headers(client)
    template = await publish_template(client, headers, title="下架成片")
    _user_body, auth = await _user(client, "13700137006")
    image_key = await upload_pet(client, auth)
    created = await client.post(
        "/v1/generations",
        headers=auth,
        json={"template_id": template["id"], "image_key": image_key, "ownership_confirmed": True},
    )
    await advance_generation({"redis": None}, created.json()["id"])
    async with get_sessionmaker()() as session:
        task = await session.get(GenerationTask, uuid.UUID(created.json()["id"]))
        media = await session.get(Media, task.result_media_id)
        media.taken_down = True
        await session.commit()
    detail = await client.get(f"/v1/generations/{created.json()['id']}", headers=auth)
    assert detail.json()["playable"] is False
    assert detail.json()["video_url"] is None
    assert detail.json()["error_message"] == "该作品已下架"
    account = None
    async with get_sessionmaker()() as session:
        account = await session.scalar(select(CreditAccount).where(CreditAccount.balance == 10, CreditAccount.frozen == 0))
    assert account is not None
