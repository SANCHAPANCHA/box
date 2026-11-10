"""💰 Auto-buy: settings, per-account switches, positions with sell buttons, trade history."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import Nav, Trd
from bot.keyboards import btn, cancel_kb, nav_row, pagination_row
from bot.states import TradingInput
from bot.ui import clamp_page, leave_state, pages, show
from core.config import Settings
from models import Trade, TradeSide, TradeStatus, User
from repositories import AccountRepository
from services.chain import EXPLORER, apply_slippage, token_url, tx_url
from services.formatting import esc
from services.timeutils import format_dt
from services.trading import (
    SELL_PERCENTS,
    fmt_eth,
    fmt_tokens,
    fmt_wei,
    positions,
    queue_sell,
    recent_trades,
    short_addr,
    today_usage,
)

log = logging.getLogger(__name__)
router = Router(name="trading")

ACC_PAGE = 8
POS_PAGE = 4


@dataclass(frozen=True, slots=True)
class Field:
    attr: str
    title: str
    kind: str  # eth | pct | int
    presets: tuple[str, ...]
    nullable: bool
    hint: str
    minimum: Decimal
    maximum: Decimal


FIELDS: dict[str, Field] = {
    "amount": Field(
        "buy_amount_eth", "💸 Сумма покупки", "eth", ("0.001", "0.005", "0.01", "0.02", "0.05", "0.1"), False,
        "Сколько ETH тратить на одну автопокупку.", Decimal("0.0001"), Decimal("100"),
    ),
    "mcap": Field(
        "max_mcap_eth", "📈 Макс. капитализация", "eth", ("3", "5", "10", "20", "50", "100"), True,
        "Бот не купит, если капитализация токена (по цене вашей покупки) выше этого значения. "
        "Для ориентира: новый токен Pons стартует примерно с 1.7 ETH.",
        Decimal("0.1"), Decimal("1000000"),
    ),
    "slip": Field(
        "slippage_bps", "🎯 Проскальзывание", "pct", ("3", "5", "10", "15", "20", "30"), False,
        "Насколько итог может быть хуже котировки. Если цена изменится сильнее, транзакция отменится "
        "и ETH не потратится (только газ).",
        Decimal("0.1"), Decimal("50"),
    ),
    "tax": Field(
        "max_creator_tax_bps", "🧾 Макс. налог создателя", "pct", ("0", "2", "5", "10"), True,
        "Создатель токена Pons может взять дополнительный налог с каждой сделки. Токены с налогом выше "
        "лимита не покупаются.",
        Decimal("0"), Decimal("50"),
    ),
    "daily": Field(
        "daily_limit_eth", "📅 Дневной лимит", "eth", ("0.01", "0.05", "0.1", "0.25", "0.5", "1"), True,
        "Максимум ETH на автопокупки за день (по вашему часовому поясу).", Decimal("0.0001"), Decimal("1000"),
    ),
    "count": Field(
        "max_buys_per_day", "🔢 Покупок в день", "int", ("1", "3", "5", "10", "20", "50"), True,
        "Максимум автопокупок за день.", Decimal("1"), Decimal("1000"),
    ),
}  # fmt: skip


def field_value(user: User, field: Field) -> str:
    value = getattr(user, field.attr)
    if value is None:
        return "без лимита"
    if field.kind == "eth":
        return f"{fmt_eth(value)} ETH"
    if field.kind == "pct":
        return f"{Decimal(value) / 100:g}%"
    return str(value)


def parse_field_value(field: Field, raw: str) -> Decimal | int | None:
    """Returns the DB value or raises ValueError."""
    raw = raw.strip().replace(",", ".").replace("%", "").replace("ETH", "").replace("eth", "").strip()
    if field.nullable and raw.lower() in ("none", "off", "нет", "-", "без лимита"):
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(raw) from exc
    if not (field.minimum <= number <= field.maximum):
        raise ValueError(raw)
    if field.kind == "pct":
        return int(number * 100)
    if field.kind == "int":
        if number != number.to_integral_value():
            raise ValueError(raw)
        return int(number)
    return number.quantize(Decimal("0.000000000000000001"))


# ---------------------------------------------------------------- main menu


def not_available(settings: Settings, user: User) -> str | None:
    if not settings.trading_configured:
        return (
            "💰 <b>Автопокупка не настроена</b>\n\n"
            "Чтобы бот мог покупать токены Pons в сети Robinhood Chain, на сервере в файле <code>.env</code> "
            "нужно указать:\n"
            "<code>TRADING_ENABLED=true</code>\n"
            "<code>WALLET_PRIVATE_KEY=0x…</code> — ключ <b>отдельного</b> кошелька только для бота\n\n"
            "Затем выполните <code>docker compose up -d</code>. Подробности — в README."
        )
    if not settings.can_trade(user.telegram_id):
        return "⛔ У вас нет доступа к торговле (TRADING_ALLOWED_USER_IDS)."
    return None


async def render_menu(session: AsyncSession, user: User, settings: Settings, chain) -> tuple[str, InlineKeyboardMarkup]:
    blocked = not_available(settings, user)
    if blocked:
        return blocked, InlineKeyboardMarkup(inline_keyboard=[nav_row()])
    try:
        balance = f"{fmt_wei(await chain.eth_balance())} ETH"
    except Exception as exc:  # RPC hiccup must not break the menu
        log.warning("balance lookup failed: %s", exc)
        balance = "недоступен (ошибка RPC)"
    count, spent = await today_usage(session, user)
    status = "🟢 ВКЛЮЧЕНА" if user.autobuy_enabled else "🔴 ВЫКЛЮЧЕНА"
    lines = [
        "💰 <b>Автопокупка токенов Pons</b> · Robinhood Chain",
        "",
        f"Статус: <b>{status}</b>",
        f'👛 Кошелёк: <a href="{EXPLORER}/address/{chain.address}">{short_addr(chain.address)}</a>',
        f"💎 Баланс: <b>{balance}</b>",
        "",
    ]
    lines += [f"{f.title}: <b>{field_value(user, f)}</b>" for f in FIELDS.values()]
    lines += [
        f"📊 Сегодня: {count} покупок на {fmt_wei(spent)} ETH",
        "",
        "Как работает: если отслеживаемый аккаунт публикует <b>собственный пост</b> с адресом контракта "
        "(например <code>$CA:0x…</code>), бот проверяет, что это токен Pons, применяет лимиты и покупает. "
        "Каждый токен покупается один раз.",
    ]
    kb = InlineKeyboardBuilder()
    kb.row(btn("🔴 Выключить автопокупку" if user.autobuy_enabled else "🟢 Включить автопокупку", Trd(action="toggle")))
    kb.row(
        btn(FIELDS["amount"].title, Trd(action="field", value="amount")),
        btn(FIELDS["mcap"].title, Trd(action="field", value="mcap")),
    )
    kb.row(
        btn(FIELDS["slip"].title, Trd(action="field", value="slip")),
        btn(FIELDS["tax"].title, Trd(action="field", value="tax")),
    )
    kb.row(
        btn(FIELDS["daily"].title, Trd(action="field", value="daily")),
        btn(FIELDS["count"].title, Trd(action="field", value="count")),
    )
    kb.row(btn("👥 Аккаунты для автопокупки", Trd(action="accounts")))
    kb.row(btn("💼 Позиции", Trd(action="positions")), btn("📜 Сделки", Trd(action="history")))
    kb.row(*nav_row())
    return "\n".join(lines), kb.as_markup()


@router.callback_query(Nav.filter(F.to == "trading"))
@router.callback_query(Trd.filter(F.action == "menu"))
async def cb_menu(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User, settings: Settings, chain=None
) -> None:
    await leave_state(state)
    await show(call, *await render_menu(session, db_user, settings, chain))


@router.callback_query(Trd.filter(F.action == "toggle"))
async def cb_toggle(call: CallbackQuery, session: AsyncSession, db_user: User, settings: Settings, chain=None) -> None:
    if not_available(settings, db_user):
        await call.answer("Торговля недоступна", show_alert=True)
        return
    db_user.autobuy_enabled = not db_user.autobuy_enabled
    await session.flush()
    await call.answer("Автопокупка включена" if db_user.autobuy_enabled else "Автопокупка выключена")
    await show(call, *await render_menu(session, db_user, settings, chain))


# ---------------------------------------------------------------- settings fields


def render_field(user: User, key: str) -> tuple[str, InlineKeyboardMarkup]:
    field = FIELDS[key]
    unit = {"eth": " ETH", "pct": "%", "int": ""}[field.kind]
    kb = InlineKeyboardBuilder()
    for preset in field.presets:
        kb.button(text=f"{preset}{unit}", callback_data=Trd(action="set", value=f"{key}={preset}"))
    kb.adjust(3)
    extra = [btn("✏️ Ввести", Trd(action="input", value=key))]
    if field.nullable:
        extra.append(btn("♾ Без лимита", Trd(action="set", value=f"{key}=none")))
    kb.row(*extra)
    kb.row(*nav_row(Trd(action="menu")))
    text = f"<b>{field.title}</b>\n\nСейчас: <b>{field_value(user, field)}</b>\n\n{field.hint}"
    return text, kb.as_markup()


@router.callback_query(Trd.filter(F.action == "field"))
async def cb_field(call: CallbackQuery, callback_data: Trd, db_user: User) -> None:
    if callback_data.value not in FIELDS:
        await call.answer()
        return
    await show(call, *render_field(db_user, callback_data.value))


@router.callback_query(Trd.filter(F.action == "set"))
async def cb_set(
    call: CallbackQuery, callback_data: Trd, session: AsyncSession, db_user: User, settings: Settings, chain=None
) -> None:
    key, _, raw = callback_data.value.partition("=")
    field = FIELDS.get(key)
    try:
        if field is None:
            raise ValueError(raw)
        value = None if raw == "none" and field.nullable else parse_field_value(field, raw)
    except ValueError:
        await call.answer("Недопустимое значение", show_alert=True)
        return
    setattr(db_user, field.attr, value)
    await session.flush()
    await call.answer(f"{field.title}: {field_value(db_user, field)}")
    await show(call, *await render_menu(session, db_user, settings, chain))


@router.callback_query(Trd.filter(F.action == "input"))
async def cb_input(call: CallbackQuery, callback_data: Trd, state: FSMContext) -> None:
    field = FIELDS.get(callback_data.value)
    if field is None:
        await call.answer()
        return
    await state.set_state(TradingInput.value)
    await state.update_data(trading_field=callback_data.value)
    example = {"eth": "0.015", "pct": "12.5", "int": "7"}[field.kind]
    limit = " или «нет» — без лимита" if field.nullable else ""
    await show(
        call,
        f"✏️ <b>{field.title}</b>\n\nОтправьте число, например <code>{example}</code>{limit}.\n"
        f"Допустимо: {field.minimum:g} – {field.maximum:g}.",
        cancel_kb(Trd(action="field", value=callback_data.value)),
    )


@router.message(TradingInput.value, F.text)
async def on_input(
    message: Message, state: FSMContext, session: AsyncSession, db_user: User, settings: Settings, chain=None
) -> None:
    key = (await state.get_data()).get("trading_field")
    field = FIELDS.get(key or "")
    if field is None:
        await leave_state(state)
        return
    try:
        value = parse_field_value(field, message.text)
    except ValueError:
        await message.answer(
            f"❌ Не удалось распознать значение. Допустимо: {field.minimum:g} – {field.maximum:g}.",
            reply_markup=cancel_kb(Trd(action="field", value=key)),
        )
        return
    setattr(db_user, field.attr, value)
    await session.flush()
    await leave_state(state)
    await message.answer(f"✅ {field.title}: <b>{field_value(db_user, field)}</b>")
    await show(message, *await render_menu(session, db_user, settings, chain))


# ---------------------------------------------------------------- accounts


async def render_accounts(session: AsyncSession, user: User, page: int) -> tuple[str, InlineKeyboardMarkup]:
    repo = AccountRepository(session)
    total = await repo.count_subscriptions(user.id)
    kb = InlineKeyboardBuilder()
    page = clamp_page(page, total, ACC_PAGE)
    for view in await repo.list_subscriptions(user.id, offset=page * ACC_PAGE, limit=ACC_PAGE):
        icon = "🤖" if view.subscription.autobuy_enabled else "⛔"
        kb.row(btn(f"{icon} @{view.account.username}", Trd(action="acc", id=view.account.id, page=page)))
    kb.row(*pagination_row(page, pages(total, ACC_PAGE), lambda p: Trd(action="accounts", page=p)))
    kb.row(*nav_row(Trd(action="menu")))
    text = (
        "👥 <b>Аккаунты для автопокупки</b>\n\n"
        "🤖 — покупать токены из постов этого аккаунта, ⛔ — не покупать.\n"
        "Нажмите на аккаунт, чтобы переключить."
    )
    if total == 0:
        text += "\n\nНет отслеживаемых аккаунтов."
    return text, kb.as_markup()


@router.callback_query(Trd.filter(F.action == "accounts"))
async def cb_accounts(call: CallbackQuery, callback_data: Trd, session: AsyncSession, db_user: User) -> None:
    await show(call, *await render_accounts(session, db_user, callback_data.page))


@router.callback_query(Trd.filter(F.action == "acc"))
async def cb_account(call: CallbackQuery, callback_data: Trd, session: AsyncSession, db_user: User) -> None:
    sub = await AccountRepository(session).get_subscription(db_user.id, callback_data.id)
    if sub is not None:
        sub.autobuy_enabled = not sub.autobuy_enabled
        await session.flush()
    await show(call, *await render_accounts(session, db_user, callback_data.page))


# ---------------------------------------------------------------- positions and selling


def sell_row(trade_id: int, label: str = "") -> list:
    return [btn(f"{label}{pct}%", Trd(action="sell_ask", id=trade_id, value=str(pct))) for pct in SELL_PERCENTS]


@router.callback_query(Trd.filter(F.action == "positions"))
async def cb_positions(
    call: CallbackQuery, callback_data: Trd, session: AsyncSession, db_user: User, settings: Settings, chain=None
) -> None:
    if not_available(settings, db_user):
        await call.answer("Торговля недоступна", show_alert=True)
        return
    items = await positions(session, db_user)
    page = clamp_page(callback_data.page, len(items), POS_PAGE)
    lines = ["💼 <b>Позиции</b> (купленные ботом токены)", ""]
    kb = InlineKeyboardBuilder()
    shown = 0
    for trade in items[page * POS_PAGE : (page + 1) * POS_PAGE]:
        symbol = trade.token_symbol or short_addr(trade.token_address)
        try:
            balance = await chain.token_balance(trade.token_address)
            value = ""
            if balance > 0:
                launch = await chain.get_launch(trade.token_address)
                if launch.venue:
                    quote = await chain.quote_sell(launch, balance)
                    value = f" ≈ <b>{fmt_wei(quote.amount_out)} ETH</b>"
            holding = f"{fmt_tokens(balance)}{value}"
        except Exception as exc:
            log.warning("position lookup failed for %s: %s", trade.token_address, exc)
            balance, holding = 1, "баланс недоступен"
        lines.append(
            f'🪙 <a href="{token_url(trade.token_address)}">{esc(symbol)}</a> · '
            f"куплено за {fmt_wei(trade.amount_in)} ETH "
            f"{format_dt(trade.created_at, db_user.timezone, '%d.%m %H:%M')}\n     👛 {holding}"
        )
        if balance > 0:
            kb.row(*sell_row(trade.id, f"{symbol[:8]} "))
        shown += 1
    if not shown:
        lines.append("Пока нет купленных токенов.")
    else:
        lines += ["", "Кнопки — продать часть или всё (с подтверждением)."]
    kb.row(*pagination_row(page, pages(len(items), POS_PAGE), lambda p: Trd(action="positions", page=p)))
    kb.row(*nav_row(Trd(action="menu")))
    await show(call, "\n".join(lines), kb.as_markup())


@router.callback_query(Trd.filter(F.action == "sell_ask"))
async def cb_sell_ask(
    call: CallbackQuery, callback_data: Trd, session: AsyncSession, db_user: User, settings: Settings, chain=None
) -> None:
    if not_available(settings, db_user):
        await call.answer("Торговля недоступна", show_alert=True)
        return
    buy = await session.get(Trade, callback_data.id)
    pct = int(callback_data.value or 0)
    if buy is None or buy.user_id != db_user.id or buy.side is not TradeSide.BUY or pct not in SELL_PERCENTS:
        await call.answer("Позиция не найдена", show_alert=True)
        return
    try:
        launch = await chain.get_launch(buy.token_address)
        balance = await chain.token_balance(launch.token)
        amount = balance if pct == 100 else balance * pct // 100
        if amount <= 0:
            await call.answer("На кошельке нет этого токена", show_alert=True)
            return
        if launch.venue is None:
            await call.answer("Токен сейчас переходит в пул Uniswap, попробуйте через минуту", show_alert=True)
            return
        quote = await chain.quote_sell(launch, amount)
    except Exception as exc:  # RPC or chain errors: keep the menu usable
        log.warning("sell quote failed: %s", exc)
        await call.answer("Не удалось получить котировку, попробуйте ещё раз", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(
        btn(f"✅ Продать {pct}%", Trd(action="sell_yes", id=buy.id, value=str(pct))),
        btn("❌ Отмена", Trd(action="positions")),
    )
    kb.row(*nav_row(Trd(action="menu")))
    minimum = apply_slippage(quote.amount_out, db_user.slippage_bps)
    await show(
        call,
        f"⚠️ <b>Продать {pct}% {esc(launch.symbol)}?</b>\n\n"
        f"📤 Количество: {fmt_tokens(amount, launch.decimals)} {esc(launch.symbol)}\n"
        f"💰 Ожидается: ≈<b>{fmt_wei(quote.amount_out)} ETH</b>\n"
        f"🛡 Минимум с учётом проскальзывания {db_user.slippage_bps / 100:g}%: {fmt_wei(minimum)} ETH\n\n"
        "Цена может измениться до исполнения.",
        kb.as_markup(),
    )


@router.callback_query(Trd.filter(F.action == "sell_yes"))
async def cb_sell_yes(
    call: CallbackQuery, callback_data: Trd, session: AsyncSession, db_user: User, settings: Settings
) -> None:
    if not_available(settings, db_user):
        await call.answer("Торговля недоступна", show_alert=True)
        return
    sell = await queue_sell(session, db_user, callback_data.id, int(callback_data.value or 0))
    if sell is None:
        await call.answer("Позиция не найдена", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(btn("💼 Позиции", Trd(action="positions")), btn("📜 Сделки", Trd(action="history")))
    kb.row(*nav_row(Trd(action="menu")))
    await show(
        call,
        f"⏳ Продажа {sell.sell_pct}% {esc(sell.token_symbol or short_addr(sell.token_address))} отправлена. "
        "Результат придёт отдельным сообщением.",
        kb.as_markup(),
    )


# ---------------------------------------------------------------- history

STATUS_ICON = {
    TradeStatus.PENDING: "⏳",
    TradeStatus.PROCESSING: "⏳",
    TradeStatus.SUBMITTED: "📡",
    TradeStatus.SUCCESS: "✅",
    TradeStatus.FAILED: "❌",
    TradeStatus.SKIPPED: "⏭",
}


@router.callback_query(Trd.filter(F.action == "history"))
async def cb_history(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    lines = ["📜 <b>Последние сделки</b>", ""]
    for t in await recent_trades(session, db_user):
        symbol = esc(t.token_symbol or short_addr(t.token_address))
        when = format_dt(t.created_at, db_user.timezone, "%d.%m %H:%M")
        if t.side is TradeSide.BUY:
            what = f"покупка {symbol} за {fmt_wei(t.amount_in)} ETH"
        else:
            what = f"продажа {t.sell_pct}% {symbol}" + (
                f" → {fmt_wei(t.amount_out)} ETH" if t.status is TradeStatus.SUCCESS else ""
            )
        line = f"{STATUS_ICON[t.status]} {when} · {what}"
        if t.tx_hash:
            line += f' · <a href="{tx_url(t.tx_hash)}">tx</a>'
        if t.status in (TradeStatus.SKIPPED, TradeStatus.FAILED) and t.reason:
            line += f"\n     <i>{esc(t.reason[:150])}</i>"
        lines.append(line)
    if len(lines) == 2:
        lines.append("Сделок пока не было.")
    kb = InlineKeyboardBuilder()
    kb.row(btn("🔄 Обновить", Trd(action="history")))
    kb.row(*nav_row(Trd(action="menu")))
    await show(call, "\n".join(lines), kb.as_markup())


def trade_keyboard(sell_trade_id: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if sell_trade_id:
        kb.row(*sell_row(sell_trade_id, "Продать "))
    kb.row(btn("💼 Позиции", Trd(action="positions")), btn("💰 Автопокупка", Trd(action="menu")))
    return kb.as_markup()
