"""⚙️ Settings: poll interval, quiet hours, timezone, grouping, error alerts."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Nav, Notif, Sett
from bot.keyboards import btn, cancel_kb, nav_row, on_off
from bot.states import SettingsInput
from bot.ui import human_interval, leave_state, show
from core.config import Settings
from models import Subscription, User, XAccount
from repositories import AccountRepository
from services.formatting import esc
from services.timeutils import describe_tz, parse_timezone_input, utcnow

router = Router(name="settings")

INTERVALS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
APP_RATE_LIMIT_15M = 10_000  # GET /2/users/:id/tweets, per app (docs.x.com rate limits)
TZ_PRESETS = [
    "Europe/Moscow", "Europe/Kyiv", "Europe/Minsk", "Europe/Berlin", "Europe/London", "Asia/Almaty",
    "Asia/Tashkent", "Asia/Yekaterinburg", "Asia/Novosibirsk", "Asia/Vladivostok", "America/New_York", "UTC",
]  # fmt: skip


def quiet_label(user: User) -> str:
    window = f"{user.quiet_start_hour:02d}:00–{user.quiet_end_hour:02d}:00"
    return f"{window} (вкл)" if user.quiet_hours_enabled else f"выкл ({window})"


def render_menu(user: User) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"⏱ Частота проверки: <b>{human_interval(user.poll_interval_seconds)}</b>\n"
        f"🌙 Тихие часы: <b>{quiet_label(user)}</b>\n"
        f"🌍 Часовой пояс: <b>{esc(describe_tz(user.timezone))}</b>\n"
        f"🧩 Группировка уведомлений: <b>{'вкл' if user.grouping_enabled else 'выкл'}</b>\n"
        f"⚠️ Уведомления об ошибках мониторинга: <b>{'вкл' if user.error_notifications_enabled else 'выкл'}</b>\n\n"
        "Все настройки сохраняются в базе данных и переживают перезапуск."
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn("⏱ Частота проверки", Sett(action="interval_menu")))
    kb.row(btn("🌙 Тихие часы", Sett(action="quiet_menu")), btn("🌍 Часовой пояс", Sett(action="tz_menu")))
    kb.row(btn(f"🧩 Группировка: {on_off(user.grouping_enabled)}", Sett(action="grouping")))
    kb.row(btn(f"⚠️ Ошибки мониторинга: {on_off(user.error_notifications_enabled)}", Sett(action="errors")))
    kb.row(btn("🔔 Типы уведомлений и аккаунты", Notif(action="menu")))
    kb.row(*nav_row())
    return text, kb.as_markup()


@router.callback_query(Nav.filter(F.to == "settings"))
@router.callback_query(Sett.filter(F.action == "menu"))
async def cb_menu(call: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await leave_state(state)
    await show(call, *render_menu(db_user))


@router.callback_query(Sett.filter(F.action == "grouping"))
async def cb_grouping(call: CallbackQuery, db_user: User) -> None:
    db_user.grouping_enabled = not db_user.grouping_enabled
    await show(call, *render_menu(db_user))


@router.callback_query(Sett.filter(F.action == "errors"))
async def cb_errors(call: CallbackQuery, db_user: User) -> None:
    db_user.error_notifications_enabled = not db_user.error_notifications_enabled
    await show(call, *render_menu(db_user))


# ---------------------------------------------------------------- interval


@router.callback_query(Sett.filter(F.action == "interval_menu"))
async def cb_interval_menu(call: CallbackQuery, session: AsyncSession, db_user: User, settings: Settings) -> None:
    accounts = await AccountRepository(session).count_monitored()
    per_15m = accounts * 900 / max(db_user.poll_interval_seconds, 1)
    max_accounts_5s = APP_RATE_LIMIT_15M * 5 // 900
    lines = [
        "⏱ <b>Частота проверки</b>",
        "",
        f"Сейчас: <b>{human_interval(db_user.poll_interval_seconds)}</b>",
        "",
        "Каждая проверка аккаунта — 1 запрос к X API (лента пользователя с <code>since_id</code>).",
        "• Лимит X: 10 000 запросов / 15 мин на приложение.",
        f"• Сейчас ≈ <b>{per_15m:.0f}</b> запросов / 15 мин для {accounts} аккаунтов.",
        f"• При 5 сек лимита хватает примерно на {max_accounts_5s} аккаунтов.",
        "• Оплата pay-per-use начисляется за каждую полученную публикацию (≈ $0.005). "
        "Тарифицируются ли пустые ответы, X не документирует — проверьте расход в console.x.com.",
    ]
    if per_15m > APP_RATE_LIMIT_15M:
        lines.append(
            "\n⚠️ <b>Лимит X будет превышен</b>: часть проверок будет откладываться до сброса лимита. "
            "Увеличьте интервал или уменьшите число аккаунтов."
        )
    if settings.min_poll_interval_seconds > min(INTERVALS):
        lines.append(
            f"\nℹ️ Интервалы меньше {settings.min_poll_interval_seconds} сек отключены на сервере. "
            "Чтобы включить 5 и 10 сек, укажите в .env <code>MIN_POLL_INTERVAL_SECONDS=5</code> "
            "и выполните <code>docker compose up -d</code>."
        )
    lines.append("\nЧем меньше интервал, тем быстрее приходят уведомления и срабатывает автопокупка.")
    kb = InlineKeyboardBuilder()
    for seconds in INTERVALS:
        if seconds < settings.min_poll_interval_seconds:
            continue
        mark = "• " if seconds == db_user.poll_interval_seconds else ""
        kb.button(text=f"{mark}{human_interval(seconds)}", callback_data=Sett(action="interval", value=str(seconds)))
    kb.adjust(3)
    kb.row(*nav_row(Sett(action="menu")))
    await show(call, "\n".join(lines), kb.as_markup())


@router.callback_query(Sett.filter(F.action == "interval"))
async def cb_interval(
    call: CallbackQuery, callback_data: Sett, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    seconds = int(callback_data.value or 0)
    if seconds not in INTERVALS or seconds < settings.min_poll_interval_seconds:
        await call.answer("Недопустимое значение", show_alert=True)
        return
    db_user.poll_interval_seconds = seconds
    # Apply right away instead of waiting for the old, possibly longer, interval to elapse.
    subscribed = select(Subscription.x_account_id).where(Subscription.user_id == db_user.id)
    await session.execute(
        update(XAccount)
        .where(XAccount.id.in_(subscribed), XAccount.next_check_at > utcnow())
        .values(next_check_at=utcnow())
    )
    await call.answer(f"Частота проверки: {human_interval(seconds)}")
    await show(call, *render_menu(db_user))


# ---------------------------------------------------------------- quiet hours


def render_quiet(user: User) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🌙 <b>Тихие часы</b>\n\n"
        f"Статус: <b>{'включены' if user.quiet_hours_enabled else 'выключены'}</b>\n"
        f"Период: <b>{user.quiet_start_hour:02d}:00 – {user.quiet_end_hour:02d}:00</b> "
        f"({esc(user.timezone)})\n\n"
        "В тихие часы уведомления не приходят. Мониторинг продолжается, а накопленные события будут "
        "отправлены сразу после окончания периода (одной сводкой, если включена группировка)."
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn("🔕 Выключить" if user.quiet_hours_enabled else "🌙 Включить", Sett(action="quiet_toggle")))
    kb.row(
        btn(f"🕐 Начало: {user.quiet_start_hour:02d}:00", Sett(action="quiet_pick", value="start")),
        btn(f"🕗 Конец: {user.quiet_end_hour:02d}:00", Sett(action="quiet_pick", value="end")),
    )
    kb.row(*nav_row(Sett(action="menu")))
    return text, kb.as_markup()


@router.callback_query(Sett.filter(F.action == "quiet_menu"))
async def cb_quiet_menu(call: CallbackQuery, db_user: User) -> None:
    await show(call, *render_quiet(db_user))


@router.callback_query(Sett.filter(F.action == "quiet_toggle"))
async def cb_quiet_toggle(call: CallbackQuery, db_user: User) -> None:
    db_user.quiet_hours_enabled = not db_user.quiet_hours_enabled
    await show(call, *render_quiet(db_user))


@router.callback_query(Sett.filter(F.action == "quiet_pick"))
async def cb_quiet_pick(call: CallbackQuery, callback_data: Sett, db_user: User) -> None:
    which = callback_data.value
    current = db_user.quiet_start_hour if which == "start" else db_user.quiet_end_hour
    kb = InlineKeyboardBuilder()
    for hour in range(24):
        mark = "•" if hour == current else ""
        kb.button(text=f"{mark}{hour:02d}:00", callback_data=Sett(action=f"quiet_{which}", value=str(hour)))
    kb.adjust(6)
    kb.row(*nav_row(Sett(action="quiet_menu")))
    title = "начала" if which == "start" else "окончания"
    await show(call, f"🌙 Выберите час {title} тихих часов ({esc(db_user.timezone)}):", kb.as_markup())


@router.callback_query(Sett.filter(F.action.in_({"quiet_start", "quiet_end"})))
async def cb_quiet_set(call: CallbackQuery, callback_data: Sett, db_user: User) -> None:
    hour = int(callback_data.value or 0) % 24
    if callback_data.action == "quiet_start":
        db_user.quiet_start_hour = hour
    else:
        db_user.quiet_end_hour = hour
    if db_user.quiet_start_hour == db_user.quiet_end_hour:
        await call.answer("Начало и конец совпадают — тихие часы не будут действовать.", show_alert=True)
    await show(call, *render_quiet(db_user))


# ---------------------------------------------------------------- timezone


@router.callback_query(Sett.filter(F.action == "tz_menu"))
async def cb_tz_menu(call: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await leave_state(state)
    kb = InlineKeyboardBuilder()
    for i, tz in enumerate(TZ_PRESETS):
        mark = "• " if tz == db_user.timezone else ""
        kb.button(text=f"{mark}{tz}", callback_data=Sett(action="tz", value=str(i)))
    kb.adjust(2)
    kb.row(btn("✏️ Ввести вручную", Sett(action="tz_manual")))
    kb.row(*nav_row(Sett(action="menu")))
    await show(
        call,
        f"🌍 <b>Часовой пояс</b>\n\nСейчас: <b>{esc(describe_tz(db_user.timezone))}</b>\n\n"
        "Используется для времени в уведомлениях, тихих часов и статистики «за сегодня».",
        kb.as_markup(),
    )


@router.callback_query(Sett.filter(F.action == "tz"))
async def cb_tz(call: CallbackQuery, callback_data: Sett, db_user: User) -> None:
    index = int(callback_data.value or 0)
    if 0 <= index < len(TZ_PRESETS):
        db_user.timezone = TZ_PRESETS[index]
        await call.answer(f"Часовой пояс: {db_user.timezone}")
    await show(call, *render_menu(db_user))


@router.callback_query(Sett.filter(F.action == "tz_manual"))
async def cb_tz_manual(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsInput.timezone)
    await show(
        call,
        "✏️ Отправьте часовой пояс в формате IANA (например <code>Asia/Bangkok</code>) "
        "или смещение (например <code>UTC+3</code>, <code>-05:00</code>).",
        cancel_kb(Sett(action="tz_menu")),
    )


@router.message(SettingsInput.timezone, F.text)
async def on_tz_input(message: Message, state: FSMContext, db_user: User) -> None:
    tz = parse_timezone_input(message.text)
    if tz is None:
        await message.answer(
            "❌ Не удалось распознать часовой пояс. Пример: <code>Europe/Moscow</code> или <code>UTC+3</code>.",
            reply_markup=cancel_kb(Sett(action="tz_menu")),
        )
        return
    db_user.timezone = tz
    await leave_state(state)
    await message.answer(f"✅ Часовой пояс сохранён: <b>{esc(describe_tz(tz))}</b>")
    await show(message, *render_menu(db_user))
