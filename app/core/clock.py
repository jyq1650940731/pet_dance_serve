from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def start_of_today_cn() -> datetime:
    now = datetime.now(CN)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)
