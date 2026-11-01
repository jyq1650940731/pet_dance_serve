import asyncio
import hashlib
import hmac
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from app.core.errors import AppError, StorageNotFound


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str


def safe_key(key: str) -> str:
    if not key or len(key) > 240 or key.startswith("/") or ".." in key.split("/"):
        raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-")
    if any(ch not in allowed for ch in key):
        raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")
    return key


def norm_content_type(value: str | None) -> str:
    if not value:
        return ""
    return value.split(";", 1)[0].strip().lower()


class Storage:
    async def presign_put(self, key: str, content_type: str, max_size: int, expires: int = 600) -> str:
        raise NotImplementedError

    async def presign_get(self, key: str, expires: int = 3600) -> str:
        raise NotImplementedError

    async def public_url(self, key: str) -> str:
        raise NotImplementedError

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        raise NotImplementedError

    async def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    async def head(self, key: str) -> ObjectInfo | None:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def delete_prefix(self, prefix: str) -> None:
        raise NotImplementedError

    async def copy_object(self, src: str, dst: str) -> None:
        info = await self.head(src)
        if info is None:
            raise StorageNotFound(src)
        data = await self.get_bytes(src)
        await self.put_bytes(dst, data, info.content_type or "application/octet-stream")


class LocalStorage(Storage):
    def __init__(self, root: Path, public_base: str, secret: str):
        self.root = root
        self.public_base = public_base.rstrip("/")
        self.secret = secret.encode()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")
        return path

    def _ctype_path(self, key: str) -> Path:
        return self._path(key).with_name(self._path(key).name + ".__ctype")

    def _sign(self, method: str, key: str, exp: int, content_type: str, max_size: int) -> str:
        message = f"{method}\n{key}\n{exp}\n{content_type}\n{max_size}".encode()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def _url(self, key: str, method: str, content_type: str, max_size: int, expires: int) -> str:
        exp = int(time.time()) + expires
        sig = self._sign(method, key, exp, content_type, max_size)
        query = urlencode({"exp": exp, "method": method, "sig": sig, "ctype": content_type, "max": max_size})
        return f"{self.public_base}/_local-storage/object/{key}?{query}"

    def verify(self, method: str, key: str, exp: int, sig: str, content_type: str, max_size: int) -> None:
        safe_key(key)
        if int(exp) < int(time.time()):
            raise AppError(403, "UPLOAD_EXPIRED", "上传地址已过期")
        expected = self._sign(method, key, int(exp), content_type, int(max_size))
        if not hmac.compare_digest(expected, sig):
            raise AppError(403, "UPLOAD_EXPIRED", "上传地址无效")

    async def presign_put(self, key: str, content_type: str, max_size: int, expires: int = 600) -> str:
        safe_key(key)
        return self._url(key, "PUT", content_type, max_size, expires)

    async def presign_get(self, key: str, expires: int = 3600) -> str:
        safe_key(key)
        return self._url(key, "GET", "", 0, expires)

    async def public_url(self, key: str) -> str:
        safe_key(key)
        if not key.startswith("templates/"):
            return await self.presign_get(key, expires=3600)
        return f"{self.public_base}/_local-storage/public/{key}"

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._ctype_path(key).write_text(content_type, encoding="utf-8")

    async def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageNotFound(key)
        return path.read_bytes()

    async def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        ctype_path = self._ctype_path(key)
        content_type = ctype_path.read_text(encoding="utf-8") if ctype_path.is_file() else "application/octet-stream"
        return ObjectInfo(size=path.stat().st_size, content_type=content_type)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()
        ctype = path.with_name(path.name + ".__ctype")
        if ctype.is_file():
            ctype.unlink()

    async def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix or prefix == "templates" or ".." in prefix.split("/"):
            raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")
        folder = (self.root / prefix).resolve()
        if not folder.is_relative_to(self.root.resolve()):
            raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")
        if folder.exists():
            shutil.rmtree(folder)


class OssStorage(Storage):
    def __init__(self, endpoint: str, bucket_name: str, key_id: str, key_secret: str, cdn_base: str):
        try:
            import oss2
        except ImportError as exc:
            raise RuntimeError("使用阿里云 OSS 需要安装 oss2") from exc
        if not endpoint or not bucket_name or not key_id or not key_secret:
            raise RuntimeError("OSS 配置不完整")
        auth = oss2.Auth(key_id, key_secret)
        self.bucket = oss2.Bucket(auth, endpoint, bucket_name)
        self.cdn_base = cdn_base.rstrip("/")
        self._oss2 = oss2

    async def presign_put(self, key: str, content_type: str, max_size: int, expires: int = 600) -> str:
        safe_key(key)
        del max_size

        def _sign() -> str:
            return self.bucket.sign_url("PUT", key, expires, headers={"Content-Type": content_type}, slash_safe=True)

        return await asyncio.to_thread(_sign)

    async def presign_get(self, key: str, expires: int = 3600) -> str:
        safe_key(key)

        def _sign() -> str:
            return self.bucket.sign_url("GET", key, expires, slash_safe=True)

        return await asyncio.to_thread(_sign)

    async def public_url(self, key: str) -> str:
        safe_key(key)
        if key.startswith("templates/") and self.cdn_base:
            return self.cdn_base + "/" + quote(key, safe="/")
        return await self.presign_get(key, expires=7 * 24 * 3600)

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        safe_key(key)
        await asyncio.to_thread(self.bucket.put_object, key, data, {"Content-Type": content_type})

    async def get_bytes(self, key: str) -> bytes:
        safe_key(key)

        def _read() -> bytes:
            try:
                result = self.bucket.get_object(key)
                return result.read()
            except self._oss2.exceptions.NoSuchKey as exc:
                raise StorageNotFound(key) from exc

        return await asyncio.to_thread(_read)

    async def head(self, key: str) -> ObjectInfo | None:
        safe_key(key)

        def _head() -> ObjectInfo | None:
            try:
                meta = self.bucket.head_object(key)
            except self._oss2.exceptions.NoSuchKey:
                return None
            return ObjectInfo(size=meta.content_length, content_type=meta.content_type or "")

        return await asyncio.to_thread(_head)

    async def delete(self, key: str) -> None:
        safe_key(key)
        await asyncio.to_thread(self.bucket.delete_object, key)

    async def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/") + "/"
        if prefix.startswith("/") or ".." in prefix.split("/"):
            raise AppError(400, "MEDIA_KEY_INVALID", "文件地址无效")

        def _wipe() -> None:
            keys = [obj.key for obj in self._oss2.ObjectIterator(self.bucket, prefix=prefix)]
            if keys:
                self.bucket.batch_delete_objects(keys)

        await asyncio.to_thread(_wipe)

    async def copy_object(self, src: str, dst: str) -> None:
        safe_key(src)
        safe_key(dst)

        def _copy() -> None:
            self.bucket.copy_object(self.bucket.bucket_name, src, dst)

        await asyncio.to_thread(_copy)


_storage: Storage | None = None


def build_storage() -> Storage:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorage(Path(settings.storage_local_dir), settings.storage_public_base, settings.signing_secret())
    return OssStorage(
        settings.oss_endpoint,
        settings.oss_bucket,
        settings.oss_access_key_id,
        settings.oss_access_key_secret,
        settings.oss_cdn_base,
    )


def set_storage(storage: Storage) -> None:
    global _storage
    _storage = storage


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = build_storage()
    return _storage
