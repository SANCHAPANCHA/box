"""Shared helpers for handlers."""

from __future__ import annotations

import contextlib
import math

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def show(
    target: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    preview_url: str | None = None,
) -> None:
    """Edits the menu message for callbacks, sends a new one for messages."""
    preview = LinkPreviewOptions(url=preview_url, prefer_small_media=True) if preview_url else NO_PREVIEW
    if isinstance(target, CallbackQuery):
        message = target.message
        if isinstance(message, Message):
            try:
                await message.edit_text(text, reply_markup=markup, link_preview_options=preview)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc):
                    await message.answer(text, reply_markup=markup, link_preview_options=preview)
        with contextlib.suppress(TelegramBadRequest):
            await target.answer()
    else:
        await target.answer(text, reply_markup=markup, link_preview_options=preview)


async def leave_state(state: FSMContext) -> None:
    """Exit the input state but keep stored data (history filters)."""
    await state.set_state(None)


def pages(total: int, size: int) -> int:
    return max(1, math.ceil(total / size))


def clamp_page(page: int, total: int, size: int) -> int:
    return min(max(page, 0), pages(total, size) - 1)


def human_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч"
    if seconds % 60 == 0:
        return f"{seconds // 60} мин"
    return f"{seconds // 60} мин {seconds % 60} сек"
