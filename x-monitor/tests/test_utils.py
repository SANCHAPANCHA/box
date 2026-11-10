from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from core.logging import RedactingFormatter, redact
from models import Event, EventType
from services.account_manager import parse_username
from services.formatting import TELEGRAM_TEXT_LIMIT, render_digest, render_event
from services.timeutils import is_quiet_time, parse_date_range, parse_timezone_input


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@elonmusk", "elonmusk"),
        ("elonmusk", "elonmusk"),
        ("https://x.com/elonmusk", "elonmusk"),
        ("https://twitter.com/ElonMusk/", "ElonMusk"),
        ("x.com/elonmusk?s=21", "elonmusk"),
        ("https://mobile.twitter.com/jack/status/20", "jack"),
        ("https://www.x.com/@nasa", "nasa"),
        ("https://x.com/home", None),
        ("https://x.com/i/web/status/1", None),
        ("waytoolongusername_x", None),
        ("bad-name", None),
        ("https://facebook.com/elonmusk", None),
        ("", None),
    ],
)
def test_parse_username(raw: str, expected: str | None) -> None:
    assert parse_username(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Europe/Moscow", "Europe/Moscow"),
        ("europe/moscow", "Europe/Moscow"),
        ("UTC", "UTC"),
        ("UTC+3", "Etc/GMT-3"),
        ("+03:00", "Etc/GMT-3"),
        ("GMT-5", "Etc/GMT+5"),
        ("Mars/Olympus", None),
        ("UTC+3:30", None),
    ],
)
def test_parse_timezone(raw: str, expected: str | None) -> None:
    assert parse_timezone_input(raw) == expected


def test_quiet_hours_across_midnight() -> None:
    # 23:00-08:00 Moscow (UTC+3)
    def at(hour_utc: int) -> datetime:
        return datetime(2026, 11, 10, hour_utc, 30, tzinfo=UTC)

    assert is_quiet_time(at(21), "Europe/Moscow", True, 23, 8)  # 00:30 local
    assert is_quiet_time(at(20), "Europe/Moscow", True, 23, 8)  # 23:30 local
    assert not is_quiet_time(at(5), "Europe/Moscow", True, 23, 8)  # 08:30 local
    assert not is_quiet_time(at(21), "Europe/Moscow", False, 23, 8)
    assert is_quiet_time(at(10), "UTC", True, 9, 17)
    assert not is_quiet_time(at(17), "UTC", True, 9, 17)


def test_parse_date_range() -> None:
    start, end = parse_date_range("01.11.2026-10.11.2026", "UTC")
    assert start == datetime(2026, 11, 1, tzinfo=UTC)
    assert end == datetime(2026, 11, 11, tzinfo=UTC)
    single = parse_date_range("05.11.2026", "Europe/Moscow")
    assert single[0] == datetime(2026, 11, 4, 21, tzinfo=UTC)
    assert parse_date_range("10.11.2026-01.11.2026", "UTC") is None
    assert parse_date_range("hello", "UTC") is None


def _event(**kw) -> Event:
    base = dict(
        post_id=1,
        x_account_id=1,
        event_type=EventType.POST,
        author_username="elonmusk",
        author_name="Elon <Musk>",
        text="Hello & <b>world</b>",
        url="https://x.com/elonmusk/status/1",
        posted_at=datetime(2026, 11, 10, 9, 30, tzinfo=UTC),
        media=[],
    )
    base.update(kw)
    return Event(**base)


def test_render_post_escapes_html_and_localizes_time() -> None:
    msg = render_event(_event(), "Europe/Moscow")
    assert "НОВАЯ АКТИВНОСТЬ X" in msg.text
    assert "Elon &lt;Musk&gt;" in msg.text
    assert "Hello &amp; &lt;b&gt;world&lt;/b&gt;" in msg.text
    assert "10.11.2026 12:30" in msg.text
    assert "Новый пост" in msg.text
    assert msg.buttons[0] == ("🔗 Открыть публикацию", "https://x.com/elonmusk/status/1")


def test_render_reply_repost_quote() -> None:
    reply = render_event(
        _event(event_type=EventType.REPLY, in_reply_to_username="NASA", referenced_text="We have liftoff"), "UTC"
    )
    assert "Ответ на публикацию @NASA" in reply.text and "We have liftoff" in reply.text

    own = render_event(_event(event_type=EventType.REPLY, in_reply_to_username="elonmusk"), "UTC")
    assert "собственной ветки" in own.text

    repost = render_event(
        _event(
            event_type=EventType.REPOST,
            referenced_author_username="SpaceX",
            referenced_author_name="SpaceX",
            referenced_text="Launch!",
            referenced_url="https://x.com/SpaceX/status/9",
        ),
        "UTC",
    )
    assert "Оригинальный пост" in repost.text and "Launch!" in repost.text
    assert repost.buttons[0][1] == "https://x.com/SpaceX/status/9"

    quote = render_event(
        _event(
            event_type=EventType.QUOTE,
            text="My take",
            referenced_text="Original",
            referenced_author_username="bob",
            referenced_url="https://x.com/bob/status/5",
            media=[{"type": "photo", "url": "https://pbs.twimg.com/a.jpg"}, {"type": "video", "url": None}],
        ),
        "UTC",
    )
    assert "Комментарий" in quote.text and "My take" in quote.text and "Original" in quote.text
    assert "1 фото, 1 видео" in quote.text
    assert quote.preview_url == "https://pbs.twimg.com/a.jpg"


def test_render_limits() -> None:
    huge = render_event(_event(text="x" * 10000, referenced_text="y" * 10000, event_type=EventType.QUOTE), "UTC")
    assert len(huge.text) <= TELEGRAM_TEXT_LIMIT
    chunks = render_digest([_event(post_id=i, text="z" * 400) for i in range(40)], "UTC")
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_TEXT_LIMIT for c in chunks)


def test_log_redaction() -> None:
    token = "123456789:AAH-very_secret_token_value_123456789"
    text = f"POST https://api.telegram.org/bot{token}/sendMessage Authorization: Bearer AAAAAAAAAAAAAAAAAAAAsecret"
    out = redact(text)
    assert token not in out and "AAAAAAAAAAAAAAAAAAAAsecret" not in out
    assert "postgresql+asyncpg://user:***@db" in redact("postgresql+asyncpg://user:pass@db/x")

    formatter = RedactingFormatter("%(message)s", ["my-explicit-secret"])
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "value=%s", ("my-explicit-secret",), None)
    assert formatter.format(record) == "value=***"
