from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import Acc, Nav, Noop


def btn(text: str, cb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def nav_row(back: object | None = None) -> list[InlineKeyboardButton]:
    row = []
    if back is not None:
        row.append(btn("⬅️ Назад", back))
    row.append(btn("🏠 Главное меню", Nav(to="main")))
    return row


def pagination_row(page: int, pages: int, make) -> list[InlineKeyboardButton]:
    """``make(page)`` builds the callback for a page."""
    if pages <= 1:
        return []
    row = []
    row.append(btn("◀️", make(page - 1)) if page > 0 else btn("·", Noop()))
    row.append(btn(f"{page + 1}/{pages}", Noop()))
    row.append(btn("▶️", make(page + 1)) if page + 1 < pages else btn("·", Noop()))
    return row


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn("👤 Мои аккаунты", Acc(action="list")), btn("➕ Добавить аккаунт", Nav(to="add")))
    kb.row(btn("🗑 Удалить аккаунт", Acc(action="del_list")), btn("📊 Статистика", Nav(to="stats")))
    kb.row(btn("📜 История", Nav(to="history")), btn("🔔 Уведомления", Nav(to="notif")))
    kb.row(btn("⚙️ Настройки", Nav(to="settings")), btn("ℹ️ Помощь", Nav(to="help")))
    kb.row(btn("💰 Автопокупка токенов", Nav(to="trading")))
    return kb.as_markup()


def only_nav(back: object | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[nav_row(back)])


def cancel_kb(back: object) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", back)], [btn("🏠 Главное меню", Nav(to="main"))]])


def on_off(value: bool) -> str:
    return "✅" if value else "❌"
