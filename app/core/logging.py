import logging

from app.domain.phones import mask_text


class PhoneMaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_text(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: mask_text(value) if isinstance(value, str) else value for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(mask_text(value) if isinstance(value, str) else value for value in record.args)
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in root.handlers:
        if not any(isinstance(item, PhoneMaskFilter) for item in handler.filters):
            handler.addFilter(PhoneMaskFilter())
