from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Nav, Noop
from bot.keyboards import main_menu, only_nav
from bot.ui import human_interval, leave_state, show
from models import AccountStatus, User
from repositories import AccountRepository

router = Router(name="start")

HELP_TEXT = """ℹ️ <b>Помощь</b>

<b>Что умеет бот</b>
Следит за аккаунтами X (Twitter) и присылает уведомления, когда отслеживаемый пользователь:
📝 публикует новый пост
💬 отвечает на публикацию (Reply / комментарий)
🔁 делает репост
🗨 публикует цитату (Quote Post)

<b>Как начать</b>
1. «➕ Добавить аккаунт» → отправьте @username или ссылку на профиль.
2. Бот проверит профиль и начнёт мониторинг. Старые публикации не присылаются — только новые.

<b>Разделы</b>
👤 <b>Мои аккаунты</b> — список, статус, пауза, удаление.
🔔 <b>Уведомления</b> — какие типы событий и от каких аккаунтов присылать, группировка.
⚙️ <b>Настройки</b> — частота проверки, тихие часы, часовой пояс, оповещения об ошибках.
📜 <b>История</b> — все обнаруженные действия с фильтрами и поиском.
📊 <b>Статистика</b> — за день, неделю, месяц.

<b>Ограничения X API</b>
• Данные берутся из официального X API v2 (лента пользователя). Это опрос, а не push: задержка ≈ частоте проверки.
• Закрытые (protected) аккаунты недоступны.
• Лайки и ответы, которые X скрыл или удалил до проверки, не отслеживаются.
• X API платный (pay-per-use): ≈ $0.005 за каждую полученную публикацию.

<b>Команды</b>
/start — главное меню
/menu — главное меню
/cancel — отменить ввод
/help — эта справка"""


async def main_menu_text(session: AsyncSession, user: User) -> str:
    repo = AccountRepository(session)
    total = await repo.count_subscriptions(user.id)
    by_status = await repo.count_by_status(user.id)
    problems = by_status.get(AccountStatus.ERROR, 0) + by_status.get(AccountStatus.UNAVAILABLE, 0)
    lines = [
        "🛰 <b>X Monitor</b>",
        "",
        f"👥 Отслеживается аккаунтов: <b>{total}</b>",
        f"🔔 Уведомления: <b>{'включены' if user.notifications_enabled else 'выключены'}</b>",
        f"⏱ Частота проверки: <b>{human_interval(user.poll_interval_seconds)}</b>",
    ]
    if problems:
        lines.append(f"⚠️ Аккаунтов с ошибками: <b>{problems}</b>")
    lines += ["", "Выберите раздел:"]
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await leave_state(state)
    name = message.from_user.first_name if message.from_user else ""
    await message.answer(
        f"👋 Привет, {name}!\n\n"
        "Я слежу за аккаунтами X (Twitter) и сразу сообщаю о новых постах, ответах, репостах и цитатах."
    )
    await show(message, await main_menu_text(session, db_user), main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await leave_state(state)
    await show(message, await main_menu_text(session, db_user), main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await leave_state(state)
    await message.answer("❌ Действие отменено.")
    await show(message, await main_menu_text(session, db_user), main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await show(message, HELP_TEXT, only_nav())


@router.callback_query(Nav.filter(F.to == "main"))
async def cb_main(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await leave_state(state)
    await show(call, await main_menu_text(session, db_user), main_menu())


@router.callback_query(Nav.filter(F.to == "help"))
async def cb_help(call: CallbackQuery) -> None:
    await show(call, HELP_TEXT, only_nav())


@router.callback_query(Noop.filter())
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()
