from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable

_PATTERNS = [
    # Telegram bot tokens, also inside api.telegram.org/bot<token>/ URLs
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9%._~+/=-]{16,}"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)"),
    re.compile(r"(?i)(redis://[^:/\s]*:)[^@\s]+(@)"),
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(r"\1***\2", text)
        elif pattern.groups == 1:
            text = pattern.sub(r"\1***", text)
        else:
            text = pattern.sub("***", text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats the full record (incl. tracebacks) and then strips secrets."""

    def __init__(self, fmt: str | None, secrets: Iterable[str], as_json: bool = False) -> None:
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self._secrets = [s for s in secrets if s]
        self._as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        if self._as_json:
            payload = {
                "ts": self.formatTime(record, self.datefmt),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return redact(json.dumps(payload, ensure_ascii=False), self._secrets)
        return redact(super().format(record), self._secrets)


def setup_logging(level: str = "INFO", secrets: Iterable[str] = (), as_json: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", secrets, as_json))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # These log full request URLs (the Telegram one contains the bot token).
    for noisy in ("httpx", "httpcore", "aiohttp.access", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
