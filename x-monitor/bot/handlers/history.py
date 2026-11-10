"""📜 History with filters (account, type, text, date) and sorting. Filters live in FSM data."""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Hist, Nav
from bot.keyboards import btn, cancel_kb, nav_row, pagination_row
from bot.states import HistoryInput
from bot.ui import clamp_page, leave_state, pages, show
from models import EventType, User
from repositories import AccountRepository, EventRepository, HistoryFilter
from services.formatting import TYPE_ICON, TYPE_SHORT, esc, truncate
from services.timeutils import format_dt, parse_date_range, period_start, utcnow

router = Router(name="history")

PAGE_SIZE = 5
ACC_PAGE = 8
KEY = "history"
PERIODS = {"day": "Сегодня", "week": "7 дней", "month": "30 дней", "all": "Всё время"}
DEFAULTS: dict = {
    "account_id": None,
    "type": None,
    "search": None,
    "period": "all",
    "from": None,
    "to": None,
    "newest": True,
}


async def get_filters(state: FSMContext) -> dict:
    data = await state.get_data()
    return {**DEFAULTS, **(data.get(KEY) or {})}


async def save_filters(state: FSMContext, filters: dict) -> None:
    await state.update_data({KEY: filters})


def to_filter(f: dict, tz: str) -> HistoryFilter:
    flt = HistoryFilter(
        account_id=f["account_id"],
        event_type=EventType(f["type"]) if f["type"] else None,
        search=f["search"],
        newest_first=f["newest"],
    )
    if f["period"] == "custom" and f["from"] and f["to"]:
        flt.date_from = datetime.fromisoformat(f["from"])
        flt.date_to = datetime.fromisoformat(f["to"])
    elif f["period"] in ("day", "week", "month"):
        flt.date_from = period_start(f["period"], utcnow(), tz)
    return flt


async def render(session: AsyncSession, user: User, f: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    events_repo = EventRepository(session)
    flt = to_filter(f, user.timezone)
    total = (await events_repo.history(user.id, flt, offset=0, limit=0))[1]
    page = clamp_page(page, total, PAGE_SIZE)
    events, total = await events_repo.history(user.id, flt, offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    account_label = "Все аккаунты"
    if f["account_id"]:
        acc = await AccountRepository(session).get(f["account_id"])
        account_label = f"@{acc.username}" if acc else "?"
    if f["period"] == "custom" and f["from"]:
        period_label = (
            f"{format_dt(datetime.fromisoformat(f['from']), user.timezone, '%d.%m.%Y')}–"
            f"{format_dt(datetime.fromisoformat(f['to']), user.timezone, '%d.%m.%Y')} (не включая)"
        )
    else:
        period_label = PERIODS.get(f["period"], "Всё время")
    type_label = TYPE_SHORT[EventType(f["type"])] if f["type"] else "Все типы"
    order_label = "Сначала новые" if f["newest"] else "Сначала старые"

    lines = [
        "📜 <b>История активности</b>",
        "",
        f"👤 {esc(account_label)} · 🏷 {type_label} · 📅 {esc(period_label)}",
        f"🔍 Поиск: {esc(f['search']) if f['search'] else '—'} · ↕️ {order_label}",
        f"Найдено: <b>{total}</b>",
    ]
    if not events:
        lines += ["", "Ничего не найдено."]
    for i, e in enumerate(events, page * PAGE_SIZE + 1):
        body = e.referenced_text if e.event_type is EventType.REPOST else e.text
        url = e.referenced_url if e.event_type is EventType.REPOST and e.referenced_url else e.url
        extra = ""
        if e.event_type is EventType.REPOST and e.referenced_author_username:
            extra = f" ← @{esc(e.referenced_author_username)}"
        elif e.event_type is EventType.REPLY and e.in_reply_to_username:
            extra = f" → @{esc(e.in_reply_to_username)}"
        lines += [
            "",
            f"<b>{i}.</b> {TYPE_ICON[e.event_type]} <b>@{esc(e.author_username)}</b>{extra}"
            f" · {format_dt(e.posted_at, user.timezone)}",
            esc(truncate(body or "", 200)) or "<i>(без текста)</i>",
            f'🔗 <a href="{esc(url)}">Открыть в X</a>',
        ]

    kb = InlineKeyboardBuilder()
    kb.row(btn("👤 Аккаунт", Hist(action="acc_menu")), btn("🏷 Тип", Hist(action="type_menu")))
    kb.row(btn("🔍 Поиск", Hist(action="search")), btn("📅 Период", Hist(action="period_menu")))
    kb.row(btn("↕️ Сортировка", Hist(action="sort", page=page)), btn("♻️ Сбросить", Hist(action="reset")))
    kb.row(*pagination_row(page, pages(total, PAGE_SIZE), lambda p: Hist(action="show", page=p)))
    kb.row(*nav_row())
    return "\n".join(lines), kb.as_markup()


async def _show(target, state: FSMContext, session: AsyncSession, user: User, page: int = 0) -> None:
    await show(target, *await render(session, user, await get_filters(state), page))


@router.callback_query(Nav.filter(F.to == "history"))
async def cb_open(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await leave_state(state)
    await _show(call, state, session, db_user)


@router.callback_query(Hist.filter(F.action == "show"))
async def cb_page(
    call: CallbackQuery, callback_data: Hist, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    await leave_state(state)
    await _show(call, state, session, db_user, callback_data.page)


@router.callback_query(Hist.filter(F.action == "reset"))
async def cb_reset(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await save_filters(state, dict(DEFAULTS))
    await _show(call, state, session, db_user)


@router.callback_query(Hist.filter(F.action == "sort"))
async def cb_sort(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    f = await get_filters(state)
    f["newest"] = not f["newest"]
    await save_filters(state, f)
    await _show(call, state, session, db_user)


# ---------------------------------------------------------------- account filter


@router.callback_query(Hist.filter(F.action == "acc_menu"))
async def cb_acc_menu(call: CallbackQuery, callback_data: Hist, session: AsyncSession, db_user: User) -> None:
    accounts = await EventRepository(session).history_accounts(db_user.id)
    page = clamp_page(callback_data.page, len(accounts), ACC_PAGE)
    kb = InlineKeyboardBuilder()
    kb.row(btn("👥 Все аккаунты", Hist(action="acc", value="")))
    for acc in accounts[page * ACC_PAGE : (page + 1) * ACC_PAGE]:
        kb.row(btn(f"@{acc.username}", Hist(action="acc", value=str(acc.id))))
    kb.row(*pagination_row(page, pages(len(accounts), ACC_PAGE), lambda p: Hist(action="acc_menu", page=p)))
    kb.row(*nav_row(Hist(action="show")))
    await show(call, "👤 Фильтр по аккаунту (включая удалённые из мониторинга):", kb.as_markup())


@router.callback_query(Hist.filter(F.action == "acc"))
async def cb_acc(
    call: CallbackQuery, callback_data: Hist, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    f = await get_filters(state)
    f["account_id"] = int(callback_data.value) if callback_data.value else None
    await save_filters(state, f)
    await _show(call, state, session, db_user)


# ---------------------------------------------------------------- type filter


@router.callback_query(Hist.filter(F.action == "type_menu"))
async def cb_type_menu(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(btn("Все типы", Hist(action="type", value="")))
    for t in EventType:
        kb.button(text=TYPE_SHORT[t], callback_data=Hist(action="type", value=t.value))
    kb.adjust(1, 2, 2)
    kb.row(*nav_row(Hist(action="show")))
    await show(call, "🏷 Фильтр по типу активности:", kb.as_markup())


@router.callback_query(Hist.filter(F.action == "type"))
async def cb_type(
    call: CallbackQuery, callback_data: Hist, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    f = await get_filters(state)
    f["type"] = callback_data.value if callback_data.value in {t.value for t in EventType} else None
    await save_filters(state, f)
    await _show(call, state, session, db_user)


# ---------------------------------------------------------------- period filter


@router.callback_query(Hist.filter(F.action == "period_menu"))
async def cb_period_menu(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    for key, label in PERIODS.items():
        kb.button(text=label, callback_data=Hist(action="period", value=key))
    kb.adjust(2)
    kb.row(btn("✏️ Указать даты", Hist(action="dates")))
    kb.row(*nav_row(Hist(action="show")))
    await show(call, "📅 Фильтр по дате публикации:", kb.as_markup())


@router.callback_query(Hist.filter(F.action == "period"))
async def cb_period(
    call: CallbackQuery, callback_data: Hist, state: FSMContext, session: AsyncSession, db_user: User
) -> None:
    f = await get_filters(state)
    f.update(period=callback_data.value if callback_data.value in PERIODS else "all", **{"from": None, "to": None})
    await save_filters(state, f)
    await _show(call, state, session, db_user)


@router.callback_query(Hist.filter(F.action == "dates"))
async def cb_dates(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(HistoryInput.dates)
    await show(
        call,
        "✏️ Отправьте период в формате <code>01.11.2026-10.11.2026</code> или одну дату <code>05.11.2026</code>.",
        cancel_kb(Hist(action="show")),
    )


@router.message(HistoryInput.dates, F.text)
async def on_dates(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    parsed = parse_date_range(message.text, db_user.timezone)
    if parsed is None:
        await message.answer(
            "❌ Неверный формат. Пример: <code>01.11.2026-10.11.2026</code>",
            reply_markup=cancel_kb(Hist(action="show")),
        )
        return
    f = await get_filters(state)
    f.update(period="custom", **{"from": parsed[0].isoformat(), "to": parsed[1].isoformat()})
    await save_filters(state, f)
    await leave_state(state)
    await _show(message, state, session, db_user)


# ---------------------------------------------------------------- text search


@router.callback_query(Hist.filter(F.action == "search"))
async def cb_search(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(HistoryInput.search)
    kb = InlineKeyboardBuilder()
    kb.row(btn("🧹 Очистить поиск", Hist(action="search_clear")))
    kb.row(btn("❌ Отмена", Hist(action="show")))
    await show(call, "🔍 Отправьте текст для поиска по содержанию публикаций:", kb.as_markup())


@router.callback_query(Hist.filter(F.action == "search_clear"))
async def cb_search_clear(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await leave_state(state)
    f = await get_filters(state)
    f["search"] = None
    await save_filters(state, f)
    await _show(call, state, session, db_user)


@router.message(HistoryInput.search, F.text)
async def on_search(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    f = await get_filters(state)
    f["search"] = message.text.strip()[:100] or None
    await save_filters(state, f)
    await leave_state(state)
    await _show(message, state, session, db_user)
