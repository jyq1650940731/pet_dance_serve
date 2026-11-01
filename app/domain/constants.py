PAGE_SIZE = 20
MAX_IMAGE_BYTES = 1_572_864  # 1.5 MiB

ACTIVE_STATUSES = ("queued", "moderating", "generating", "muxing")
TERMINAL_STATUSES = ("succeeded", "failed")

STAGE_LABELS = {
    "queued": "排队中",
    "moderating": "生成中",
    "generating": "生成中",
    "muxing": "合成中",
    "succeeded": "可播放",
    "failed": "失败",
}

# 对用户展示的失败原因。供应商原文只留在 error_detail，不下发。
PUBLIC_ERRORS = {
    "MODERATION_REJECTED": "请换一张宠物照片",
    "TIMEOUT": "生成超时，点数已退回",
    "PREVIEW_MISSING": "生成失败，点数已退回",
    "PROVIDER_FAILED": "生成失败，点数已退回",
    "MUX_FAILED": "生成失败，点数已退回",
    "CANCELLED": "已取消，点数已退回",
}

NEXT_STATUS = {
    "queued": "moderating",
    "moderating": "generating",
    "generating": "muxing",
    "muxing": "succeeded",
}

STATUS_STAMP = {
    "moderating": "moderating_at",
    "generating": "generating_at",
    "muxing": "muxing_at",
    "succeeded": "finished_at",
    "failed": "finished_at",
}


def stage_label(status: str) -> str:
    return STAGE_LABELS.get(status, status)


def public_error(code: str | None) -> str | None:
    if not code:
        return None
    return PUBLIC_ERRORS.get(code, "生成失败，点数已退回")


def assert_transition(src: str, dst: str) -> None:
    if src in TERMINAL_STATUSES:
        raise ValueError(f"任务已结束，不能从 {src} 变为 {dst}")
    if dst == "failed":
        return
    expected = NEXT_STATUS.get(src)
    if expected != dst:
        raise ValueError(f"不允许的状态变化：{src} → {dst}")
