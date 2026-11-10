from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Nav, Stat
from bot.keyboards import btn, nav_row
from bot.ui import show
from models import AccountStatus, EventType, User
from repositories import AccountRepository, EventRepository
from services.formatting import esc
from services.timeutils import period_start, start_of_day, utcnow

router = Router(name="stats")

PERIODS = {"day": "сегодня", "week": "неделю", "month": "месяц", "all": "всё время"}
BUTTONS = {"day": "День", "week": "Неделя", "month": "Месяц", "all": "Всё время"}


async def render(session: AsyncSession, user: User, period: str) -> tuple[str, InlineKeyboardMarkup]:
    period = period if period in PERIODS else "day"
    now = utcnow()
    accounts = AccountRepository(session)
    events = EventRepository(session)
    total_accounts = await accounts.count_subscriptions(user.id)
    paused = sum(1 for v in await accounts.list_subscriptions(user.id) if v.subscription.is_paused)
    by_status = await accounts.count_by_status(user.id)
    problems = by_status.get(AccountStatus.ERROR, 0) + by_status.get(AccountStatus.UNAVAILABLE, 0)
    stats = await events.period_stats(user.id, period_start(period, now, user.timezone))
    sent_today = await events.notifications_sent_since(user.id, start_of_day(now, user.timezone))

    lines = [
        f"📊 <b>Статистика за {PERIODS[period]}</b>",
        "",
        f"👥 Отслеживается аккаунтов: <b>{total_accounts}</b>"
        + (f" (на паузе: {paused})" if paused else "")
        + (f" (с ошибками: {problems})" if problems else ""),
        "",
        f"📈 Всего событий: <b>{stats.total}</b>",
        f"📝 Публикаций: <b>{stats.by_type.get(EventType.POST, 0)}</b>",
        f"💬 Ответов (Reply): <b>{stats.by_type.get(EventType.REPLY, 0)}</b>",
        f"🔁 Репостов: <b>{stats.by_type.get(EventType.REPOST, 0)}</b>",
        f"🗨 Цитат (Quote Posts): <b>{stats.by_type.get(EventType.QUOTE, 0)}</b>",
        "",
        f"📨 Уведомлений отправлено сегодня: <b>{sent_today}</b>",
    ]
    if stats.top_accounts:
        lines += ["", "🏆 <b>Самые активные:</b>"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (acc, n) in enumerate(stats.top_accounts):
            prefix = medals[i] if i < len(medals) else f"{i + 1}."
            lines.append(f"{prefix} @{esc(acc.username)} — {n}")

    kb = InlineKeyboardBuilder()
    for key, label in BUTTONS.items():
        kb.button(text=f"• {label}" if key == period else label, callback_data=Stat(period=key))
    kb.adjust(4)
    kb.row(btn("🔄 Обновить", Stat(period=period)))
    kb.row(*nav_row())
    return "\n".join(lines), kb.as_markup()


@router.callback_query(Nav.filter(F.to == "stats"))
async def cb_open(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    await show(call, *await render(session, db_user, "day"))


@router.callback_query(Stat.filter())
async def cb_period(call: CallbackQuery, callback_data: Stat, session: AsyncSession, db_user: User) -> None:
    await show(call, *await render(session, db_user, callback_data.period))
