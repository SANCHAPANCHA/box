"""Rendering of Telegram messages (HTML parse mode)."""

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, field

from models import Event, EventType
from services.timeutils import format_dt

TELEGRAM_TEXT_LIMIT = 4096

TYPE_LABELS: dict[EventType, str] = {
    EventType.POST: "📝 Новый пост",
    EventType.REPLY: "💬 Ответ (Reply)",
    EventType.REPOST: "🔁 Репост",
    EventType.QUOTE: "🗨 Цитата (Quote Post)",
}
TYPE_SHORT: dict[EventType, str] = {
    EventType.POST: "📝 Посты",
    EventType.REPLY: "💬 Ответы",
    EventType.REPOST: "🔁 Репосты",
    EventType.QUOTE: "🗨 Цитаты",
}
TYPE_ICON: dict[EventType, str] = {
    EventType.POST: "📝",
    EventType.REPLY: "💬",
    EventType.REPOST: "🔁",
    EventType.QUOTE: "🗨",
}
MEDIA_NAMES = {"photo": "фото", "video": "видео", "animated_gif": "GIF"}


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(slots=True)
class RenderedMessage:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    preview_url: str | None = None


def _author(name: str | None, username: str | None) -> str:
    if name and username:
        return f"<b>{esc(name)}</b> (@{esc(username)})"
    if username:
        return f"@{esc(username)}"
    return esc(name) or "неизвестный автор"


def _media_summary(media: list[dict]) -> str | None:
    if not media:
        return None
    counts = Counter(m.get("type", "photo") for m in media)
    return ", ".join(f"{n} {MEDIA_NAMES.get(t, t)}" for t, n in counts.items())


def _first_media_url(media: list[dict]) -> str | None:
    for item in media:
        if item.get("url"):
            return item["url"]
    return None


def render_event(event: Event, tz_name: str, text_limit: int = 1500, ref_limit: int = 700) -> RenderedMessage:
    lines = ["🚨 <b>НОВАЯ АКТИВНОСТЬ X</b>", "", f"👤 {_author(event.author_name, event.author_username)}", ""]
    lines.append(f"<b>Тип:</b> {TYPE_LABELS[event.event_type]}")
    ref_author = _author(event.referenced_author_name, event.referenced_author_username)
    open_url = event.url

    if event.event_type is EventType.REPLY:
        target = event.in_reply_to_username or event.referenced_author_username
        if target and target.lower() == event.author_username.lower():
            lines.append("↩️ Продолжение собственной ветки (ответ на свой пост)")
        elif target:
            lines.append(f"↩️ Ответ на публикацию @{esc(target)}")
        else:
            lines.append("↩️ Ответ на чужую публикацию")
        lines += ["", "💬 <b>Текст ответа:</b>", esc(truncate(event.text, text_limit))]
        if event.referenced_text:
            lines += ["", f"📌 <b>Исходная публикация</b> {ref_author}:"]
            lines.append(f"<blockquote>{esc(truncate(event.referenced_text, ref_limit))}</blockquote>")
    elif event.event_type is EventType.REPOST:
        lines += ["", f"📌 <b>Оригинальный пост</b> {ref_author}:"]
        original = event.referenced_text or event.text
        lines.append(f"<blockquote>{esc(truncate(original, text_limit))}</blockquote>")
        open_url = event.referenced_url or event.url
    elif event.event_type is EventType.QUOTE:
        lines += ["", "💬 <b>Комментарий:</b>", esc(truncate(event.text, text_limit))]
        lines += ["", f"📌 <b>Цитируемая публикация</b> {ref_author}:"]
        quoted = event.referenced_text or "(текст недоступен)"
        lines.append(f"<blockquote>{esc(truncate(quoted, ref_limit))}</blockquote>")
    else:
        lines += ["", "💬 <b>Содержание:</b>", esc(truncate(event.text, text_limit)) or "<i>(без текста)</i>"]

    media = _media_summary(event.media or [])
    if media:
        lines += ["", f"🖼 <b>Медиа:</b> {media}"]
    lines += ["", f"🕒 <b>Время публикации:</b> {format_dt(event.posted_at, tz_name)}"]
    lines += ["", f'🔗 <a href="{esc(open_url)}">Открыть публикацию</a>']

    buttons = [("🔗 Открыть публикацию", open_url)]
    if event.referenced_url and event.event_type in (EventType.QUOTE, EventType.REPLY):
        buttons.append(("📌 Исходный пост", event.referenced_url))
    buttons.append((f"👤 @{event.author_username}", f"https://x.com/{event.author_username}"))

    text = "\n".join(lines)
    if len(text) > TELEGRAM_TEXT_LIMIT:  # pathological input (very long names/links)
        return render_event(event, tz_name, text_limit // 2, ref_limit // 2)
    return RenderedMessage(text=text, buttons=buttons, preview_url=_first_media_url(event.media or []))


def render_digest(events: list[Event], tz_name: str) -> list[str]:
    """Grouped notification. Returns one or more messages that each fit Telegram's limit."""
    header = f"📦 <b>Сводка активности X</b> — {len(events)} нов. событий\n"
    chunks: list[str] = []
    current = header
    for i, event in enumerate(events, 1):
        url = event.referenced_url if event.event_type is EventType.REPOST and event.referenced_url else event.url
        body = event.referenced_text if event.event_type is EventType.REPOST else event.text
        extra = ""
        if event.event_type is EventType.REPOST and event.referenced_author_username:
            extra = f" ← @{esc(event.referenced_author_username)}"
        elif event.event_type is EventType.REPLY and event.in_reply_to_username:
            extra = f" → @{esc(event.in_reply_to_username)}"
        item = (
            f"\n{i}. {TYPE_ICON[event.event_type]} <b>@{esc(event.author_username)}</b>{extra}"
            f" · {format_dt(event.posted_at, tz_name, '%d.%m %H:%M')}\n"
            f"{esc(truncate(body or '', 220))}\n"
            f'🔗 <a href="{esc(url)}">Открыть</a>\n'
        )
        if len(current) + len(item) > TELEGRAM_TEXT_LIMIT - 50:
            chunks.append(current)
            current = header.replace("📦", "📦 (продолжение)", 1)
        current += item
    chunks.append(current)
    return chunks
