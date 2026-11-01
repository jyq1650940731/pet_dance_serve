import re

PHONE_RE = re.compile(r"1[3-9]\d{9}")


def normalize_phone(value: str) -> str:
    digits = value.strip().replace(" ", "").replace("-", "")
    if digits.startswith("+86"):
        digits = digits[3:]
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if not re.fullmatch(r"1[3-9]\d{9}", digits):
        raise ValueError("请输入中国大陆手机号")
    return digits


def mask_phone(phone: str | None) -> str:
    if not phone or len(phone) < 7:
        return ""
    return phone[:3] + "****" + phone[-4:]


def mask_text(text: str) -> str:
    return PHONE_RE.sub(lambda match: mask_phone(match.group(0)), text)
