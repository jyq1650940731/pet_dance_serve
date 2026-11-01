import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security import hash_password
from app.models import AuditLog, StaffUser
from app.samples import tiny_mp4
from app.seed import seed_templates, template_id
from tests.helpers import admin_headers, bearer, login, publish_template, signed_request


async def test_draft_stays_hidden_until_publish_and_prompts_stay_on_the_server(client):
    headers = await admin_headers(client)
    created = await client.post(
        "/admin-api/v1/templates",
        headers=headers,
        json={
            "title": "草稿模板",
            "category": "comedy",
            "pet_scope": "cat",
            "prompt": "PROMPT_SECRET_请勿下发",
            "negative_prompt": "不要文字",
        },
    )
    assert created.status_code == 201, created.text
    template_id_value = created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["prompt"] == "PROMPT_SECRET_请勿下发"

    public = await client.get("/v1/templates")
    assert public.status_code == 200
    assert public.json()["total"] == 0
    hidden = await client.get(f"/v1/templates/{template_id_value}")
    assert hidden.status_code == 404

    published = await publish_template(client, headers, title="客厅样片", sort=1)
    listed = await client.get("/v1/templates")
    assert listed.json()["total"] == 1
    assert listed.json()["featured_id"] == published["id"]
    assert "PROMPT_SECRET" not in listed.text
    detail = await client.get(f"/v1/templates/{published['id']}")
    assert detail.status_code == 200
    assert set(detail.json()) == {
        "id",
        "title",
        "subtitle",
        "category",
        "cover_url",
        "preview_url",
        "duration_sec",
        "aspect_ratio",
        "credit_cost",
        "pet_scope",
        "sort",
    }
    assert detail.json()["pet_scope"] == "any"
    cover = await signed_request(client, "GET", detail.json()["cover_url"])
    assert cover.status_code == 200
    assert cover.content[:2] == b"\xff\xd8"

    copied = await client.post(f"/admin-api/v1/templates/{published['id']}/duplicate", headers=headers)
    assert copied.status_code == 201, copied.text
    assert copied.json()["status"] == "draft"
    assert copied.json()["title"].endswith("副本")
    still_one = await client.get("/v1/templates")
    assert still_one.json()["total"] == 1

    offline = await client.patch(
        f"/admin-api/v1/templates/{published['id']}",
        headers=headers,
        json={"status": "unpublished"},
    )
    assert offline.status_code == 200, offline.text
    assert (await client.get("/v1/templates")).json()["total"] == 0

    async with get_sessionmaker()() as session:
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "template.create" in actions
    assert "template.publish" in actions or "template.create" in actions
    assert "template.duplicate" in actions
    assert "template.unpublish" in actions


async def test_catalog_seed_sorts_featured_and_filters_category(client):
    from app.main import app

    async with get_sessionmaker()() as session:
        await seed_templates(session, app.state.storage)
        await session.commit()
    listed = await client.get("/v1/templates")
    body = listed.json()
    assert body["total"] == 8
    assert body["page_size"] == 20
    assert body["featured_id"] == str(template_id("客厅蹦迪"))
    assert "保持参考图" not in listed.text
    dance = await client.get("/v1/templates", params={"category": "dance"})
    titles = [item["title"] for item in dance.json()["items"]]
    assert titles == ["客厅蹦迪", "窗边慢摇"]
    assert dance.json()["featured_id"] == str(template_id("客厅蹦迪"))
    scopes = {item["title"]: item["pet_scope"] for item in body["items"]}
    assert scopes["窗边慢摇"] == "cat"
    assert scopes["小跑过来"] == "dog"


async def test_pagination_and_bad_login(client):
    headers = await admin_headers(client)
    for index in range(21):
        response = await client.post(
            "/admin-api/v1/templates",
            headers=headers,
            json={
                "title": f"模板{index:02d}",
                "category": "daily",
                "pet_scope": "dog",
                "prompt": "动作",
                "negative_prompt": "不要文字",
                "sort": index,
                "status": "draft",
            },
        )
        assert response.status_code == 201, response.text
    page = await client.get("/admin-api/v1/templates", headers=headers, params={"page": 2, "status": "draft"})
    assert page.status_code == 200
    assert page.json()["total"] == 21
    assert len(page.json()["items"]) == 1

    bad = await client.post("/admin-api/v1/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert bad.status_code == 401
    assert bad.json()["error"]["message"] == "用户名或密码错误"
    missing = await client.post("/admin-api/v1/auth/login", json={"username": "nobody", "password": "wrong-password"})
    assert missing.json()["error"]["message"] == "用户名或密码错误"


async def test_operator_can_edit_templates_and_disabled_staff_cannot_login(client):
    headers = await admin_headers(client)
    async with get_sessionmaker()() as session:
        session.add(
            StaffUser(
                id=uuid.uuid4(),
                username="editor",
                password_hash=hash_password("editor-pass-10"),
                role="operator",
                status="active",
            )
        )
        session.add(
            StaffUser(
                id=uuid.uuid4(),
                username="frozen",
                password_hash=hash_password("frozen-pass-10"),
                role="operator",
                status="disabled",
            )
        )
        await session.commit()
    logged = await client.post("/admin-api/v1/auth/login", json={"username": "editor", "password": "editor-pass-10"})
    assert logged.status_code == 200
    assert logged.json()["role"] == "operator"
    created = await client.post(
        "/admin-api/v1/templates",
        headers=bearer(logged.json()["access_token"]),
        json={
            "title": "运营草稿",
            "category": "daily",
            "pet_scope": "any",
            "prompt": "动作",
            "negative_prompt": "不要文字",
        },
    )
    assert created.status_code == 201, created.text
    disabled = await client.post("/admin-api/v1/auth/login", json={"username": "frozen", "password": "frozen-pass-10"})
    assert disabled.status_code == 401
    user = await login(client, "13900139000")
    rejected = await client.get("/admin-api/v1/templates", headers=bearer(user.json()["access_token"]))
    assert rejected.status_code == 401
    assert headers["Authorization"].startswith("Bearer ")
    assert tiny_mp4()[4:8] == b"ftyp"
