import io

from PIL import Image, ImageOps

from app.core.errors import AppError
from app.domain.constants import MAX_IMAGE_BYTES

Image.MAX_IMAGE_PIXELS = 8_000_000

IMAGE_MESSAGE = "图片需为 9:16 的 JPEG，长边不超过 1280，大小不超过 1.5MB"


def validate_pet_image(data: bytes) -> tuple[int, int]:
    if len(data) < 32 or len(data) > MAX_IMAGE_BYTES or not data.startswith(b"\xff\xd8"):
        raise AppError(400, "IMAGE_INVALID", IMAGE_MESSAGE)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG":
                raise AppError(400, "IMAGE_INVALID", IMAGE_MESSAGE)
            transposed = ImageOps.exif_transpose(image) or image
            width, height = transposed.size
    except AppError:
        raise
    except Exception as exc:
        raise AppError(400, "IMAGE_INVALID", IMAGE_MESSAGE) from exc
    if height <= width or max(width, height) > 1280:
        raise AppError(400, "IMAGE_INVALID", IMAGE_MESSAGE)
    if abs((width / height) - (9 / 16)) > 0.02:
        raise AppError(400, "IMAGE_INVALID", IMAGE_MESSAGE)
    return width, height
