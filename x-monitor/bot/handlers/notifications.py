"""🔔 Notifications: event types, global switch, per-account selection, grouping."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Nav, Notif
from bot.keyboards import btn, nav_row, on_off, pagination_row
from bot.ui import clamp_page, pages, show
from models import EventType, Subscription, User
from repositories import AccountRepository
from services.formatting import TYPE_SHORT

router = Router(name="notifications")

ACC_PAGE = 8
TYPE_ATTR = {
    EventType.POST: "notify_post",
    EventType.REPLY: "notify_reply",
    EventType.REPOST: "notify_repost",
    EventType.QUOTE: "notify_quote",
}


def render_menu(user: User) -> tuple[str, InlineKeyboardMarkup]:
    state = "включены ✅" if user.notifications_enabled else "выключены ❌"
    text = (
        "🔔 <b>Уведомления</b>\n\n"
        f"Все уведомления: <b>{state}</b>\n\n"
        "Выберите, о каких действиях сообщать. Отключённые типы всё равно сохраняются в 📜 Истории.\n\n"
        "🧩 <b>Группировка</b>: если одновременно накопилось 3+ события, они придут одной сводкой."
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(f"🔔 Все уведомления: {'ВКЛ' if user.notifications_enabled else 'ВЫКЛ'}", Notif(action="all")))
    kb.row(
        btn(f"{on_off(user.notify_post)} {TYPE_SHORT[EventType.POST]}", Notif(action="type", value="post")),
        btn(f"{on_off(user.notify_reply)} {TYPE_SHORT[EventType.REPLY]}", Notif(action="type", value="reply")),
    )
    kb.row(
        btn(f"{on_off(user.notify_repost)} {TYPE_SHORT[EventType.REPOST]}", Notif(action="type", value="repost")),
        btn(f"{on_off(user.notify_quote)} {TYPE_SHORT[EventType.QUOTE]}", Notif(action="type", value="quote")),
    )
    kb.row(btn("👥 Аккаунты для уведомлений", Notif(action="accounts")))
    kb.row(btn(f"🧩 Группировка: {on_off(user.grouping_enabled)}", Notif(action="grouping")))
    kb.row(*nav_row())
    return text, kb.as_markup()


@router.callback_query(Nav.filter(F.to == "notif"))
@router.callback_query(Notif.filter(F.action == "menu"))
async def cb_menu(call: CallbackQuery, db_user: User) -> None:
    await show(call, *render_menu(db_user))


@router.callback_query(Notif.filter(F.action == "all"))
async def cb_all(call: CallbackQuery, db_user: User) -> None:
    db_user.notifications_enabled = not db_user.notifications_enabled
    await show(call, *render_menu(db_user))


@router.callback_query(Notif.filter(F.action == "grouping"))
async def cb_grouping(call: CallbackQuery, db_user: User) -> None:
    db_user.grouping_enabled = not db_user.grouping_enabled
    await show(call, *render_menu(db_user))


@router.callback_query(Notif.filter(F.action == "type"))
async def cb_type(call: CallbackQuery, callback_data: Notif, db_user: User) -> None:
    try:
        attr = TYPE_ATTR[EventType(callback_data.value)]
    except ValueError:
        await call.answer()
        return
    setattr(db_user, attr, not getattr(db_user, attr))
    await show(call, *render_menu(db_user))


async def render_accounts(session: AsyncSession, user: User, page: int) -> tuple[str, InlineKeyboardMarkup]:
    repo = AccountRepository(session)
    total = await repo.count_subscriptions(user.id)
    kb = InlineKeyboardBuilder()
    if total == 0:
        kb.row(*nav_row(Notif(action="menu")))
        return "👥 <b>Аккаунты для уведомлений</b>\n\nНет отслеживаемых аккаунтов.", kb.as_markup()
    page = clamp_page(page, total, ACC_PAGE)
    views = await repo.list_subscriptions(user.id, offset=page * ACC_PAGE, limit=ACC_PAGE)
    for view in views:
        icon = "✅" if view.subscription.notify_enabled else "🔕"
        kb.row(btn(f"{icon} @{view.account.username}", Notif(action="acc", value=str(view.account.id), page=page)))
    kb.row(*pagination_row(page, pages(total, ACC_PAGE), lambda p: Notif(action="accounts", page=p)))
    kb.row(
        btn("✅ Включить все", Notif(action="acc_all", value="1", page=page)),
        btn("🔕 Выключить все", Notif(action="acc_all", value="0", page=page)),
    )
    kb.row(*nav_row(Notif(action="menu")))
    text = (
        "👥 <b>Аккаунты для уведомлений</b>\n\n"
        "✅ — уведомления приходят, 🔕 — только запись в историю (мониторинг продолжается).\n"
        "Нажмите на аккаунт, чтобы переключить."
    )
    return text, kb.as_markup()


@router.callback_query(Notif.filter(F.action == "accounts"))
async def cb_accounts(call: CallbackQuery, callback_data: Notif, session: AsyncSession, db_user: User) -> None:
    await show(call, *await render_accounts(session, db_user, callback_data.page))


@router.callback_query(Notif.filter(F.action == "acc"))
async def cb_account_toggle(call: CallbackQuery, callback_data: Notif, session: AsyncSession, db_user: User) -> None:
    sub = await AccountRepository(session).get_subscription(db_user.id, int(callback_data.value or 0))
    if sub is not None:
        sub.notify_enabled = not sub.notify_enabled
        await session.flush()
    await show(call, *await render_accounts(session, db_user, callback_data.page))


@router.callback_query(Notif.filter(F.action == "acc_all"))
async def cb_account_all(call: CallbackQuery, callback_data: Notif, session: AsyncSession, db_user: User) -> None:
    await session.execute(
        update(Subscription).where(Subscription.user_id == db_user.id).values(notify_enabled=callback_data.value == "1")
    )
    await show(call, *await render_accounts(session, db_user, callback_data.page))
