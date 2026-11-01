import io


def tiny_mp4() -> bytes:
    payload = b"isom\x00\x00\x00\x00isomavc1"
    box = (8 + len(payload)).to_bytes(4, "big") + b"ftyp" + payload
    free = (8).to_bytes(4, "big") + b"free"
    return box + free


def make_jpeg(width: int = 720, height: int = 1280, color: tuple[int, int, int] = (210, 150, 110)) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()
