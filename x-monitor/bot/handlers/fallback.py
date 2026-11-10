"""Catch-all handlers. This router must be included last."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.accounts import add_accounts
from bot.handlers.start import main_menu_text
from bot.keyboards import main_menu
from bot.ui import show
from models import User
from services.account_manager import AccountManager, parse_username, split_inputs

router = Router(name="fallback")


def _looks_like_profiles(text: str) -> bool:
    tokens = split_inputs(text)
    return bool(tokens) and all(
        parse_username(t) and (t.startswith("@") or "x.com" in t.lower() or "twitter.com" in t.lower()) for t in tokens
    )


@router.message(StateFilter(None), F.text)
async def on_free_text(message: Message, session: AsyncSession, db_user: User, account_manager: AccountManager) -> None:
    """A profile link or @username sent outside any dialog adds the account directly."""
    if not message.text.startswith("/") and _looks_like_profiles(message.text):
        await add_accounts(message, message.text, session, db_user, account_manager)
        return
    await show(message, await main_menu_text(session, db_user), main_menu())


@router.message(StateFilter(None))
async def on_other(message: Message, session: AsyncSession, db_user: User) -> None:
    await show(message, await main_menu_text(session, db_user), main_menu())


@router.callback_query()
async def on_stale_callback(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    await call.answer("Это меню устарело, открываю главное меню.")
    await show(call, await main_menu_text(session, db_user), main_menu())
