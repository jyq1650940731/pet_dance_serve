from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.core.errors import AppError, StorageNotFound
from app.storage import LocalStorage, norm_content_type, safe_key

router = APIRouter(prefix="/_local-storage", include_in_schema=False)


def _storage(request: Request) -> LocalStorage:
    storage = request.app.state.storage
    if not isinstance(storage, LocalStorage):
        raise AppError(404, "NOT_FOUND", "文件不存在")
    return storage


@router.get("/public/{key:path}")
async def public_object(key: str, request: Request) -> Response:
    safe_key(key)
    if not key.startswith("templates/"):
        raise AppError(404, "NOT_FOUND", "文件不存在")
    storage = _storage(request)
    try:
        data = await storage.get_bytes(key)
        info = await storage.head(key)
    except StorageNotFound as exc:
        raise AppError(404, "NOT_FOUND", "文件不存在") from exc
    return Response(
        content=data,
        media_type=(info.content_type if info else None) or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.api_route("/object/{key:path}", methods=["GET", "PUT"])
async def signed_object(
    key: str,
    request: Request,
    exp: int,
    method: str,
    sig: str,
    ctype: str = "",
    max: int = 0,
) -> Response:
    storage = _storage(request)
    if request.method != method:
        raise AppError(403, "UPLOAD_EXPIRED", "上传地址无效")
    storage.verify(method, key, exp, sig, ctype, max)
    if request.method == "GET":
        try:
            data = await storage.get_bytes(key)
            info = await storage.head(key)
        except StorageNotFound as exc:
            raise AppError(404, "NOT_FOUND", "文件不存在") from exc
        return Response(content=data, media_type=(info.content_type if info else None) or "application/octet-stream")
    content_type = norm_content_type(request.headers.get("content-type"))
    if content_type != norm_content_type(ctype):
        raise AppError(400, "UPLOAD_INVALID", "上传类型不正确")
    body = await request.body()
    if not body or len(body) > max:
        raise AppError(413, "UPLOAD_TOO_LARGE", "文件超过大小限制")
    await storage.put_bytes(key, body, content_type)
    return Response(status_code=200)
