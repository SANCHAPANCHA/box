from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Acc, Hist, Nav
from bot.handlers.start import main_menu_text
from bot.keyboards import btn, cancel_kb, main_menu, nav_row, pagination_row
from bot.states import AddAccount
from bot.ui import clamp_page, leave_state, pages, show
from models import AccountStatus, EventType, Subscription, User, XAccount
from repositories import AccountRepository, EventRepository
from services.account_manager import AccountManager, AddResult, AddStatus, split_inputs
from services.formatting import TYPE_SHORT, esc
from services.timeutils import format_dt

router = Router(name="accounts")

PAGE_SIZE = 5
MAX_PER_MESSAGE = 10

ADD_PROMPT = (
    "➕ <b>Добавление аккаунта</b>\n\n"
    "Отправьте ссылку на профиль X или username, например:\n"
    "• <code>@elonmusk</code>\n"
    "• <code>https://x.com/elonmusk</code>\n\n"
    f"Можно отправить до {MAX_PER_MESSAGE} аккаунтов через пробел или с новой строки."
)


def status_label(account: XAccount, sub: Subscription) -> str:
    if sub.is_paused:
        return "⏸ Приостановлен"
    return {
        AccountStatus.ACTIVE: "🟢 Активный мониторинг",
        AccountStatus.ERROR: "🟠 Ошибки запросов",
        AccountStatus.UNAVAILABLE: "🔴 Недоступен",
    }[account.status]


# ---------------------------------------------------------------- list


async def render_list(session: AsyncSession, user: User, page: int) -> tuple[str, InlineKeyboardMarkup]:
    repo = AccountRepository(session)
    total = await repo.count_subscriptions(user.id)
    kb = InlineKeyboardBuilder()
    if total == 0:
        kb.row(btn("➕ Добавить аккаунт", Nav(to="add")))
        kb.row(*nav_row())
        return "👤 <b>Мои аккаунты</b>\n\nСписок пуст. Добавьте первый аккаунт для мониторинга.", kb.as_markup()

    page = clamp_page(page, total, PAGE_SIZE)
    items = await repo.list_subscriptions(user.id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    lines = [f"👤 <b>Мои аккаунты</b> (всего: {total})", ""]
    for i, view in enumerate(items, page * PAGE_SIZE + 1):
        acc, sub = view.account, view.subscription
        avatar = f' · <a href="{esc(acc.avatar_url)}">🖼 аватар</a>' if acc.avatar_url else ""
        mute = " · 🔕" if not sub.notify_enabled else ""
        name = esc(acc.display_name or acc.username)
        lines.append(f'<b>{i}.</b> <a href="{esc(acc.profile_url)}">{name}</a> (@{esc(acc.username)}){avatar}')
        lines.append(f"     📊 Событий: {view.events_count} · {status_label(acc, sub)}{mute}")
        lines.append(f"     📅 Добавлен: {format_dt(sub.created_at, user.timezone, '%d.%m.%Y')}")
        kb.row(btn(f"{i}. @{acc.username}", Acc(action="view", id=acc.id, page=page)))
    lines += ["", "Нажмите на аккаунт, чтобы открыть карточку."]
    kb.row(*pagination_row(page, pages(total, PAGE_SIZE), lambda p: Acc(action="list", page=p)))
    kb.row(btn("➕ Добавить", Nav(to="add")), btn("🗑 Удалить", Acc(action="del_list", page=page)))
    kb.row(*nav_row())
    return "\n".join(lines), kb.as_markup()


@router.callback_query(Acc.filter(F.action == "list"))
async def cb_list(call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User) -> None:
    text, kb = await render_list(session, db_user, callback_data.page)
    await show(call, text, kb)


# ---------------------------------------------------------------- card


async def render_card(
    session: AsyncSession, user: User, account_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup, str | None] | None:
    sub = await AccountRepository(session).get_subscription(user.id, account_id)
    if sub is None:
        return None
    acc = sub.account
    counts = await EventRepository(session).count_for_account(acc.id)
    lines = [
        f"👤 <b>{esc(acc.display_name or acc.username)}</b>",
        f'🔗 <a href="{esc(acc.profile_url)}">@{esc(acc.username)}</a>',
        f"📡 Статус: {status_label(acc, sub)}",
        f"🔔 Уведомления: {'включены' if sub.notify_enabled else 'выключены'}",
        f"📅 Добавлен: {format_dt(sub.created_at, user.timezone)}",
        f"🕒 Последняя проверка: {format_dt(acc.last_checked_at, user.timezone, '%d.%m.%Y %H:%M:%S')}",
        "",
        f"📊 <b>Событий обнаружено: {sum(counts.values())}</b>",
        " · ".join(f"{TYPE_SHORT[t]}: {counts.get(t, 0)}" for t in EventType),
    ]
    if acc.status is not AccountStatus.ACTIVE and acc.last_error:
        lines += [
            "",
            f"⚠️ Последняя ошибка ({format_dt(acc.last_error_at, user.timezone)}):",
            f"<code>{esc(acc.last_error[:300])}</code>",
        ]
    if not acc.avatar_url:
        lines += ["", "🖼 Аватар недоступен"]

    kb = InlineKeyboardBuilder()
    if sub.is_paused:
        kb.row(btn("▶️ Возобновить мониторинг", Acc(action="resume", id=acc.id, page=page)))
    else:
        kb.row(btn("⏸ Приостановить мониторинг", Acc(action="pause", id=acc.id, page=page)))
    kb.row(
        btn(
            "🔕 Выкл. уведомления" if sub.notify_enabled else "🔔 Вкл. уведомления",
            Acc(action="mute", id=acc.id, page=page),
        ),
        btn("📜 История", Hist(action="acc", value=str(acc.id))),
    )
    kb.row(btn("🗑 Удалить", Acc(action="del_ask", id=acc.id, page=page)))
    kb.row(*nav_row(Acc(action="list", page=page)))
    return "\n".join(lines), kb.as_markup(), acc.avatar_url


async def _show_card(call: CallbackQuery, session: AsyncSession, user: User, account_id: int, page: int) -> None:
    card = await render_card(session, user, account_id, page)
    if card is None:
        await call.answer("Аккаунт не найден — возможно, он уже удалён.", show_alert=True)
        text, kb = await render_list(session, user, page)
        await show(call, text, kb)
        return
    text, kb, avatar = card
    await show(call, text, kb, preview_url=avatar)


@router.callback_query(Acc.filter(F.action == "view"))
async def cb_view(call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User) -> None:
    await _show_card(call, session, db_user, callback_data.id, callback_data.page)


@router.callback_query(Acc.filter(F.action.in_({"pause", "resume"})))
async def cb_pause(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User, account_manager: AccountManager
) -> None:
    await account_manager.set_paused(session, db_user, callback_data.id, callback_data.action == "pause")
    await session.flush()
    await _show_card(call, session, db_user, callback_data.id, callback_data.page)


@router.callback_query(Acc.filter(F.action == "mute"))
async def cb_mute(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User, account_manager: AccountManager
) -> None:
    await account_manager.toggle_notify(session, db_user, callback_data.id)
    await session.flush()
    await _show_card(call, session, db_user, callback_data.id, callback_data.page)


# ---------------------------------------------------------------- delete


@router.callback_query(Acc.filter(F.action == "del_list"))
async def cb_delete_list(call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User) -> None:
    repo = AccountRepository(session)
    total = await repo.count_subscriptions(db_user.id)
    kb = InlineKeyboardBuilder()
    if total == 0:
        kb.row(*nav_row())
        await show(call, "🗑 <b>Удаление аккаунта</b>\n\nНет отслеживаемых аккаунтов.", kb.as_markup())
        return
    page = clamp_page(callback_data.page, total, PAGE_SIZE * 2)
    for view in await repo.list_subscriptions(db_user.id, offset=page * PAGE_SIZE * 2, limit=PAGE_SIZE * 2):
        kb.row(btn(f"🗑 @{view.account.username}", Acc(action="del_ask", id=view.account.id, page=page)))
    kb.row(*pagination_row(page, pages(total, PAGE_SIZE * 2), lambda p: Acc(action="del_list", page=p)))
    kb.row(*nav_row())
    await show(
        call,
        "🗑 <b>Удаление аккаунта</b>\n\nВыберите аккаунт, который нужно убрать из мониторинга.\n"
        "История уже полученных уведомлений сохранится.",
        kb.as_markup(),
    )


@router.callback_query(Acc.filter(F.action == "del_ask"))
async def cb_delete_ask(call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User) -> None:
    sub = await AccountRepository(session).get_subscription(db_user.id, callback_data.id)
    if sub is None:
        await call.answer("Аккаунт уже удалён.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        btn("✅ Да, удалить", Acc(action="del_yes", id=callback_data.id, page=callback_data.page)),
        btn("❌ Отмена", Acc(action="view", id=callback_data.id, page=callback_data.page)),
    )
    kb.row(*nav_row())
    await show(
        call,
        f"⚠️ <b>Удалить @{esc(sub.account.username)} из мониторинга?</b>\n\n"
        "Новые действия этого аккаунта больше не будут отслеживаться.\n"
        "📜 История уже полученных уведомлений сохранится.",
        kb.as_markup(),
    )


@router.callback_query(Acc.filter(F.action == "del_yes"))
async def cb_delete_yes(
    call: CallbackQuery, callback_data: Acc, session: AsyncSession, db_user: User, account_manager: AccountManager
) -> None:
    account = await AccountRepository(session).get(callback_data.id)
    removed = await account_manager.remove(session, db_user, callback_data.id)
    kb = InlineKeyboardBuilder()
    kb.row(btn("👤 Мои аккаунты", Acc(action="list")), btn("🗑 Удалить ещё", Acc(action="del_list")))
    kb.row(*nav_row())
    name = f"@{esc(account.username)}" if account else "Аккаунт"
    text = (
        f"✅ {name} удалён из мониторинга.\n📜 История уведомлений сохранена."
        if removed
        else "ℹ️ Аккаунт уже был удалён."
    )
    await show(call, text, kb.as_markup())


# ---------------------------------------------------------------- add


@router.callback_query(Nav.filter(F.to == "add"))
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccount.waiting_input)
    await show(call, ADD_PROMPT, cancel_kb(Nav(to="main")))


def describe_result(result: AddResult) -> str:
    acc = result.account
    raw = esc(result.input)
    match result.status:
        case AddStatus.ADDED:
            return (
                "✅ <b>Аккаунт успешно добавлен!</b>\n\n"
                f"👤 Имя: {esc(acc.display_name)}\n"
                f"🔗 Username: @{esc(acc.username)}\n"
                "📡 Статус: Активный мониторинг\n\n"
                "Старые публикации не присылаются — только новые действия."
            )
        case AddStatus.ALREADY:
            return f"ℹ️ @{esc(acc.username)} уже отслеживается."
        case AddStatus.INVALID:
            return f"❌ «{raw}» — некорректный username или ссылка на профиль X."
        case AddStatus.NOT_FOUND:
            return f"❌ Аккаунт «{raw}» не найден в X."
        case AddStatus.PROTECTED:
            return f"🔒 «{raw}» — закрытый аккаунт. Его публикации недоступны через X API."
        case AddStatus.UNAVAILABLE:
            return f"🚫 «{raw}» недоступен (заблокирован или приостановлен)."
        case AddStatus.RATE_LIMITED:
            return f"⏳ «{raw}»: превышен лимит запросов к X API, попробуйте через несколько минут."
        case _:
            return (
                f"⚠️ «{raw}»: ошибка X API ({esc(result.detail)}). "
                "Проверьте X_BEARER_TOKEN и баланс кредитов в console.x.com."
            )


async def add_accounts(
    message: Message, text: str, session: AsyncSession, user: User, account_manager: AccountManager
) -> None:
    inputs = split_inputs(text)[:MAX_PER_MESSAGE]
    if not inputs:
        await message.answer("Отправьте username или ссылку на профиль.", reply_markup=cancel_kb(Nav(to="main")))
        return
    progress = await message.answer(
        "🔎 Проверяю профиль…" if len(inputs) == 1 else f"🔎 Проверяю {len(inputs)} профилей…"
    )
    results = []
    for raw in inputs:
        results.append(await account_manager.add(session, user, raw))
        await session.commit()
    kb = InlineKeyboardBuilder()
    kb.row(btn("➕ Добавить ещё", Nav(to="add")), btn("👤 Мои аккаунты", Acc(action="list")))
    kb.row(*nav_row())
    await progress.edit_text("\n\n".join(describe_result(r) for r in results), reply_markup=kb.as_markup())


@router.message(AddAccount.waiting_input, F.text)
async def on_add_input(
    message: Message, state: FSMContext, session: AsyncSession, db_user: User, account_manager: AccountManager
) -> None:
    if message.text.startswith("/"):
        await leave_state(state)
        await message.answer("Ввод отменён.")
        await show(message, await main_menu_text(session, db_user), main_menu())
        return
    await leave_state(state)
    await add_accounts(message, message.text, session, db_user, account_manager)


@router.message(AddAccount.waiting_input)
async def on_add_wrong(message: Message) -> None:
    await message.answer(
        "Отправьте username текстом, например <code>@elonmusk</code>.", reply_markup=cancel_kb(Nav(to="main"))
    )
