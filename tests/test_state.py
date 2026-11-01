from app.domain.constants import NEXT_STATUS, assert_transition
from app.domain.phones import mask_phone, normalize_phone


def test_happy_path_is_the_product_machine():
    status = "queued"
    seen = [status]
    while status != "succeeded":
        status = NEXT_STATUS[status]
        seen.append(status)
    assert seen == ["queued", "moderating", "generating", "muxing", "succeeded"]


def test_failure_is_allowed_before_the_end_and_not_after():
    for status in ("queued", "moderating", "generating", "muxing"):
        assert_transition(status, "failed")
    for status in ("succeeded", "failed"):
        try:
            assert_transition(status, "failed")
        except ValueError:
            continue
        raise AssertionError(status)


def test_phone_mask_and_normalize():
    assert mask_phone("13800138000") == "138****8000"
    assert normalize_phone("+86 138-0013-8000") == "13800138000"
    try:
        normalize_phone("12345")
    except ValueError:
        return
    raise AssertionError("短号应被拒绝")
