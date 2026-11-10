"""Auto-buy of Pons tokens whose contract address a monitored account posts, plus manual sells.

Flow
1. Monitor stores a new own post (EventType.POST) and calls TradePlanner.plan() in the same DB
   transaction: every 0x-address in the text becomes a PENDING buy for each subscriber with auto-buy on.
   The partial unique index (user_id, token_address) WHERE side='buy' makes it "once per token".
2. TradeExecutor claims PENDING rows, validates the token on-chain, applies the user's limits,
   buys, and reports to Telegram with sell buttons. Sells are queued by the bot the same way.

Crash safety: the tx hash is committed (status SUBMITTED) *before* broadcast; on restart a SUBMITTED
trade is resolved from its receipt and never re-sent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

import aiohttp
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from web3.exceptions import Web3Exception

from core.config import Settings
from core.database import SessionFactory, session_scope
from models import EventType, Subscription, Trade, TradeSide, TradeStatus, User, XAccount
from services.chain import (
    ChainError,
    Launch,
    NotPonsToken,
    Quote,
    TxResult,
    apply_slippage,
    eth_to_wei,
    token_url,
    tx_url,
    wei_to_eth,
)
from services.formatting import esc
from services.timeutils import start_of_day, utcnow
from services.x_client import XPost

log = logging.getLogger(__name__)

ADDRESS_RE = re.compile(r"(?<![0-9a-zA-Z])0x[a-fA-F0-9]{40}(?![0-9a-zA-Z])")
MAX_ADDRESSES_PER_POST = 3
SNIPE_TAX_WAIT_STEPS = 8
SELL_PERCENTS = (25, 50, 100)
RPC_RETRIES = 3
RESOLVE_EVERY_SECONDS = 60
STUCK_SUBMITTED_AFTER = timedelta(minutes=2)


def extract_contracts(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for match in ADDRESS_RE.findall(text or ""):
        seen.setdefault(match.lower(), match)
    return list(seen.values())[:MAX_ADDRESSES_PER_POST]


def fmt_eth(eth: Decimal | None, digits: int = 6) -> str:
    if eth is None:
        return "—"
    text = f"{Decimal(eth):.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_wei(wei: int | Decimal | None, digits: int = 6) -> str:
    return "—" if wei is None else fmt_eth(wei_to_eth(int(wei)), digits)


def fmt_tokens(value: int | Decimal | None, decimals: int = 18) -> str:
    if value is None:
        return "—"
    amount = Decimal(int(value)) / Decimal(10**decimals)
    if amount >= 1000:
        return f"{amount:,.0f}".replace(",", " ")
    return f"{amount:.4f}".rstrip("0").rstrip(".")


def short_addr(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


class Chain(Protocol):
    address: str

    async def get_launch(self, token: str) -> Launch: ...
    async def quote_buy(self, launch: Launch, amount_in: int) -> Quote: ...
    async def quote_sell(self, launch: Launch, tokens_in: int) -> Quote: ...
    async def buy(self, launch: Launch, amount_in: int, min_out: int, on_signed=None) -> TxResult: ...
    async def sell(self, launch: Launch, tokens_in: int, min_out: int, on_signed=None) -> TxResult: ...
    async def eth_balance(self) -> int: ...
    async def token_balance(self, token: str) -> int: ...
    async def get_receipt(self, tx_hash: str): ...


# (chat_id, html_text, buy_trade_id_for_sell_buttons | None)
TradeSend = Callable[[int, str, int | None], Awaitable[None]]


# ---------------------------------------------------------------- planning


class TradePlanner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def plan(
        self,
        session: AsyncSession,
        event_id: int,
        account: XAccount,
        post: XPost,
        subscribers: list[tuple[Subscription, User]],
    ) -> int:
        if post.type is not EventType.POST:  # only the account's own posts
            return 0
        addresses = extract_contracts(post.text)
        if not addresses:
            return 0
        created = 0
        for sub, user in subscribers:
            if not (user.autobuy_enabled and sub.autobuy_enabled and self._settings.can_trade(user.telegram_id)):
                continue
            for address in addresses:
                stmt = (
                    insert(Trade)
                    .values(
                        user_id=user.id,
                        chat_id=user.chat_id,
                        side=TradeSide.BUY,
                        status=TradeStatus.PENDING,
                        token_address=address.lower(),
                        x_account_id=account.id,
                        event_id=event_id,
                        amount_in=eth_to_wei(user.buy_amount_eth),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[Trade.user_id, Trade.token_address],
                        # must repeat the partial index predicate literally
                        index_where=text("side = 'buy'"),
                    )
                    .returning(Trade.id)
                )
                if await session.scalar(stmt):
                    created += 1
        if created:
            log.info("@%s posted %d contract address(es): %d buy(s) queued", account.username, len(addresses), created)
        return created


# ---------------------------------------------------------------- execution


@dataclass(slots=True)
class Skip(Exception):
    reason: str
    notify: bool = True


class TradeExecutor:
    def __init__(self, session_factory: SessionFactory, chain: Chain, settings: Settings, send: TradeSend) -> None:
        self._sf = session_factory
        self._chain = chain
        self._settings = settings
        self._send = send
        self._gas_reserve = eth_to_wei(settings.trade_gas_reserve_eth)
        self._retries: dict[int, int] = {}
        self._last_resolve = 0.0

    # ------------------------------------------------------------ queue

    async def recover(self) -> None:
        """Startup: un-claim rows that never got signed, resolve signed ones from their receipts."""
        async with session_scope(self._sf) as session:
            await session.execute(
                update(Trade)
                .where(Trade.status == TradeStatus.PROCESSING, Trade.tx_hash.is_(None))
                .values(status=TradeStatus.PENDING)
            )
        await self._resolve_submitted(timedelta(0))

    async def _resolve_submitted(self, min_age: timedelta) -> None:
        async with self._sf() as session:
            submitted = list(
                await session.scalars(
                    select(Trade).where(Trade.status == TradeStatus.SUBMITTED, Trade.updated_at <= utcnow() - min_age)
                )
            )
        for trade in submitted:
            receipt = None
            try:
                receipt = await self._chain.get_receipt(trade.tx_hash)
            except Exception:
                log.exception("receipt lookup failed for trade %s", trade.id)
                continue
            if receipt is None:
                status, reason = TradeStatus.FAILED, "транзакция не найдена в сети — проверьте кошелёк"
            elif receipt["status"] == 1:
                status, reason = TradeStatus.SUCCESS, "подтверждена позже (после сбоя RPC/перезапуска)"
            else:
                status, reason = TradeStatus.FAILED, "транзакция отклонена сетью"
            await self._update(trade.id, status=status, reason=reason)
            log.warning("trade %s resolved from receipt: %s", trade.id, status.value)
            symbol = esc(trade.token_symbol or short_addr(trade.token_address))
            icon = "✅" if status is TradeStatus.SUCCESS else "❌"
            await self._notify(
                trade.chat_id,
                f'{icon} Сделка по {symbol}: {esc(reason)}\n<a href="{tx_url(trade.tx_hash)}">Транзакция</a>',
                sell_trade_id=trade.id if status is TradeStatus.SUCCESS and trade.side is TradeSide.BUY else None,
            )

    async def _claim(self, limit: int = 10) -> list[int]:
        async with session_scope(self._sf) as session:
            ids = list(
                await session.scalars(
                    select(Trade.id)
                    .where(Trade.status == TradeStatus.PENDING)
                    .order_by(Trade.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if ids:
                await session.execute(
                    update(Trade).where(Trade.id.in_(ids)).values(status=TradeStatus.PROCESSING),
                    execution_options={"synchronize_session": False},
                )
            return ids

    async def run_once(self) -> int:
        # Executor runs as a single scheduler instance, so nothing of ours is in flight here.
        loop_time = asyncio.get_running_loop().time()
        if loop_time - self._last_resolve > RESOLVE_EVERY_SECONDS:
            self._last_resolve = loop_time
            await self._resolve_submitted(STUCK_SUBMITTED_AFTER)
        done = 0
        for trade_id in await self._claim():
            try:
                await self.process(trade_id)
                self._retries.pop(trade_id, None)
            except (Web3Exception, aiohttp.ClientError, TimeoutError) as exc:
                await self._rpc_error(trade_id, exc)
            except Exception as exc:  # never leave a trade in PROCESSING
                log.exception("trade %s crashed", trade_id)
                await self._fail(trade_id, f"внутренняя ошибка: {type(exc).__name__}")
            done += 1
        return done

    async def _rpc_error(self, trade_id: int, exc: Exception) -> None:
        """Transient RPC failure. Retry only if nothing was signed yet (no tx hash stored)."""
        trade = await self._load(trade_id)
        attempts = self._retries.get(trade_id, 0) + 1
        if trade.tx_hash is None and attempts <= RPC_RETRIES:
            self._retries[trade_id] = attempts
            log.warning("trade %s: RPC error (%s), retry %d/%d", trade_id, exc, attempts, RPC_RETRIES)
            await self._update(trade_id, status=TradeStatus.PENDING)
            return
        self._retries.pop(trade_id, None)
        log.error("trade %s: RPC error after retries: %s", trade_id, exc)
        if trade.tx_hash is not None:
            # Already signed/broadcast: leave SUBMITTED, recover() resolves it from the receipt.
            await self._update(trade_id, status=TradeStatus.SUBMITTED, reason=f"нет ответа RPC: {str(exc)[:200]}")
            return
        await self._fail(trade_id, f"нода Robinhood Chain недоступна: {str(exc)[:200]}")

    async def _load(self, trade_id: int) -> Trade:
        async with self._sf() as session:
            trade = await session.scalar(
                select(Trade)
                .where(Trade.id == trade_id)
                .options(selectinload(Trade.user), selectinload(Trade.account), selectinload(Trade.event))
            )
            assert trade is not None
            return trade

    async def _update(self, trade_id: int, **values) -> None:
        async with session_scope(self._sf) as session:
            await session.execute(update(Trade).where(Trade.id == trade_id).values(**values))

    async def _fail(self, trade_id: int, reason: str) -> None:
        trade = await self._load(trade_id)
        await self._update(trade_id, status=TradeStatus.FAILED, reason=reason[:1000])
        side = "Покупка" if trade.side is TradeSide.BUY else "Продажа"
        await self._notify(
            trade.chat_id,
            f"❌ <b>{side} не удалась</b>\n\n{self._token_line(trade)}\nПричина: {esc(reason[:300])}",
        )

    async def _notify(self, chat_id: int, text: str, sell_trade_id: int | None = None) -> None:
        try:
            await self._send(chat_id, text, sell_trade_id)
        except Exception:
            log.exception("failed to deliver trade message to chat %s", chat_id)

    @staticmethod
    def _token_line(trade: Trade, symbol: str | None = None) -> str:
        name = esc(symbol or trade.token_symbol or short_addr(trade.token_address))
        return f'🪙 <a href="{token_url(trade.token_address)}">{name}</a> · <code>{trade.token_address}</code>'

    async def process(self, trade_id: int) -> None:
        trade = await self._load(trade_id)
        if trade.side is TradeSide.BUY:
            await self._process_buy(trade)
        else:
            await self._process_sell(trade)

    # ------------------------------------------------------------ buy

    async def _spent_today(self, user: User) -> tuple[int, int]:
        async with self._sf() as session:
            return await today_usage(session, user)

    async def _check_buy(self, trade: Trade, user: User) -> tuple[Launch, Quote, int]:
        if utcnow() - trade.created_at > timedelta(seconds=self._settings.trade_max_age_seconds):
            raise Skip("пост слишком старый (бот был недоступен), покупка отменена")
        if not user.autobuy_enabled or not self._settings.can_trade(user.telegram_id):
            raise Skip("автопокупка выключена", notify=False)
        try:
            launch = await self._chain.get_launch(trade.token_address)
        except NotPonsToken:
            raise Skip("адрес не является токеном Pons", notify=False) from None
        await self._update(trade.id, token_symbol=launch.symbol[:64])
        trade.token_symbol = launch.symbol
        if not launch.native:
            raise Skip("токен торгуется не за ETH — не поддерживается")
        if launch.venue is None:
            raise Skip(f"токен в переходном состоянии (phase {launch.phase}), торговля недоступна")
        if user.max_creator_tax_bps is not None and launch.creator_tax_bps > user.max_creator_tax_bps:
            raise Skip(
                f"налог создателя {launch.creator_tax_bps / 100:g}% выше лимита {user.max_creator_tax_bps / 100:g}%"
            )

        amount = int(trade.amount_in or 0)
        if amount <= 0:
            raise Skip("сумма покупки не задана")
        count, spent = await self._spent_today(user)
        if user.max_buys_per_day is not None and count >= user.max_buys_per_day:
            raise Skip(f"достигнут лимит покупок в день ({user.max_buys_per_day})")
        if user.daily_limit_eth is not None and spent + amount > eth_to_wei(user.daily_limit_eth):
            raise Skip(f"превышен дневной лимит: потрачено {fmt_wei(spent)} из {fmt_eth(user.daily_limit_eth)} ETH")
        balance = await self._chain.eth_balance()
        if balance < amount + self._gas_reserve:
            raise Skip(f"недостаточно ETH: на кошельке {fmt_wei(balance)} ETH, нужно {fmt_wei(amount)} + газ")

        quote = await self._chain.quote_buy(launch, amount)
        for _ in range(SNIPE_TAX_WAIT_STEPS):  # opening tax decays to zero within seconds of launch
            if quote.snipe_tax_bps == 0:
                break
            await asyncio.sleep(1)
            quote = await self._chain.quote_buy(launch, amount)
        if quote.snipe_tax_bps > 0:
            raise Skip(f"действует налог на ранние покупки {quote.snipe_tax_bps / 100:g}%")
        if quote.amount_out <= 0:
            raise Skip("котировка вернула 0 токенов")
        mcap = quote.mcap_wei(launch.total_supply)
        if user.max_mcap_eth is not None and mcap > eth_to_wei(user.max_mcap_eth):
            raise Skip(f"капитализация ≈{fmt_wei(mcap, 2)} ETH выше лимита {fmt_eth(user.max_mcap_eth, 2)} ETH")
        return launch, quote, mcap

    async def _process_buy(self, trade: Trade) -> None:
        user = trade.user
        try:
            launch, quote, mcap = await self._check_buy(trade, user)
        except Skip as skip:
            await self._update(trade.id, status=TradeStatus.SKIPPED, reason=skip.reason)
            log.info("buy %s skipped: %s", trade.id, skip.reason)
            if skip.notify:
                await self._notify(
                    trade.chat_id,
                    f"⚠️ <b>Автопокупка пропущена</b>\n\n{self._source_line(trade)}{self._token_line(trade)}\n"
                    f"Причина: {esc(skip.reason)}",
                )
            return
        except ChainError as exc:
            await self._fail(trade.id, f"ошибка сети Robinhood Chain: {exc}")
            return

        min_out = apply_slippage(quote.amount_out, user.slippage_bps)

        async def on_signed(tx_hash: str) -> None:
            await self._update(
                trade.id, status=TradeStatus.SUBMITTED, tx_hash=tx_hash, venue=launch.venue, mcap_wei=mcap
            )

        try:
            result = await self._chain.buy(launch, quote.amount_in, min_out, on_signed=on_signed)
        except ChainError as exc:
            await self._fail(trade.id, str(exc))
            return
        if not result.success:
            await self._update(trade.id, status=TradeStatus.FAILED, reason="транзакция отклонена (revert)")
            await self._notify(
                trade.chat_id,
                f"❌ <b>Покупка не прошла</b>\n\n{self._token_line(trade)}\n"
                f"Транзакция отклонена сетью (скорее всего проскальзывание). "
                f'<a href="{tx_url(result.tx_hash)}">Транзакция</a>',
            )
            return
        await self._update(
            trade.id,
            status=TradeStatus.SUCCESS,
            amount_out=result.amount_out,
            gas_wei=result.gas_cost_wei,
            reason=None,
        )
        venue = "bonding curve Pons" if launch.venue == "curve" else "пул Uniswap v4"
        await self._notify(
            trade.chat_id,
            f"🟢 <b>Куплен токен {esc(launch.symbol)}</b>\n\n{self._source_line(trade)}{self._token_line(trade)}\n"
            f"💸 Потрачено: <b>{fmt_wei(result.amount_in)} ETH</b> (+ газ {fmt_wei(result.gas_cost_wei, 8)})\n"
            f"🎯 Получено: <b>{fmt_tokens(result.amount_out, launch.decimals)} {esc(launch.symbol)}</b>\n"
            f"📈 Капитализация при покупке: ≈{fmt_wei(mcap, 2)} ETH\n"
            f"🏦 Площадка: {venue}\n"
            f'🔗 <a href="{tx_url(result.tx_hash)}">Транзакция</a>',
            sell_trade_id=trade.id,
        )

    @staticmethod
    def _source_line(trade: Trade) -> str:
        if trade.account is None:
            return ""
        link = f' · <a href="{esc(trade.event.url)}">пост</a>' if trade.event is not None else ""
        return f"👤 @{esc(trade.account.username)}{link}\n"

    # ------------------------------------------------------------ sell

    async def _process_sell(self, trade: Trade) -> None:
        user = trade.user
        try:
            launch = await self._chain.get_launch(trade.token_address)
            if launch.venue is None:
                raise ChainError(f"токен в переходном состоянии (phase {launch.phase}), попробуйте позже")
            balance = await self._chain.token_balance(launch.token)
            pct = trade.sell_pct or 100
            amount = balance if pct >= 100 else balance * pct // 100
            if amount <= 0:
                raise ChainError("на кошельке нет этого токена")
            quote = await self._chain.quote_sell(launch, amount)
            min_out = apply_slippage(quote.amount_out, user.slippage_bps)

            async def on_signed(tx_hash: str) -> None:
                await self._update(
                    trade.id, status=TradeStatus.SUBMITTED, tx_hash=tx_hash, venue=launch.venue, amount_in=amount
                )

            result = await self._chain.sell(launch, amount, min_out, on_signed=on_signed)
        except ChainError as exc:
            await self._fail(trade.id, str(exc))
            return
        if not result.success:
            await self._update(trade.id, status=TradeStatus.FAILED, reason="транзакция отклонена (revert)")
            await self._notify(
                trade.chat_id,
                f"❌ <b>Продажа не прошла</b>\n\n{self._token_line(trade, launch.symbol)}\n"
                f'<a href="{tx_url(result.tx_hash)}">Транзакция</a> отклонена сетью (скорее всего проскальзывание).',
            )
            return
        await self._update(
            trade.id,
            status=TradeStatus.SUCCESS,
            token_symbol=launch.symbol[:64],
            amount_out=result.amount_out,
            gas_wei=result.gas_cost_wei,
        )
        left = balance - amount
        await self._notify(
            trade.chat_id,
            f"🔴 <b>Продано {pct}% {esc(launch.symbol)}</b>\n\n{self._token_line(trade, launch.symbol)}\n"
            f"📤 Продано: {fmt_tokens(amount, launch.decimals)} {esc(launch.symbol)}\n"
            f"💰 Получено: <b>{fmt_wei(result.amount_out)} ETH</b> (газ {fmt_wei(result.gas_cost_wei, 8)})\n"
            f"👛 Осталось: {fmt_tokens(left, launch.decimals)}\n"
            f'🔗 <a href="{tx_url(result.tx_hash)}">Транзакция</a>',
            sell_trade_id=trade.parent_id if left > 0 else None,
        )


# ---------------------------------------------------------------- queries for the bot


async def queue_sell(session: AsyncSession, user: User, buy_trade_id: int, pct: int) -> Trade | None:
    buy = await session.get(Trade, buy_trade_id)
    if buy is None or buy.user_id != user.id or buy.side is not TradeSide.BUY or pct not in SELL_PERCENTS:
        return None
    sell = Trade(
        user_id=user.id,
        chat_id=user.chat_id,
        side=TradeSide.SELL,
        status=TradeStatus.PENDING,
        token_address=buy.token_address,
        token_symbol=buy.token_symbol,
        x_account_id=buy.x_account_id,
        parent_id=buy.id,
        sell_pct=pct,
    )
    session.add(sell)
    await session.flush()
    return sell


async def positions(session: AsyncSession, user: User) -> list[Trade]:
    return list(
        await session.scalars(
            select(Trade)
            .where(Trade.user_id == user.id, Trade.side == TradeSide.BUY, Trade.status == TradeStatus.SUCCESS)
            .order_by(Trade.created_at.desc())
        )
    )


async def recent_trades(session: AsyncSession, user: User, limit: int = 10) -> list[Trade]:
    return list(
        await session.scalars(
            select(Trade)
            .where(Trade.user_id == user.id, Trade.reason.is_distinct_from("адрес не является токеном Pons"))
            .order_by(Trade.created_at.desc(), Trade.id.desc())
            .limit(limit)
        )
    )


async def today_usage(session: AsyncSession, user: User) -> tuple[int, int]:
    since = start_of_day(utcnow(), user.timezone)
    row = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(Trade.amount_in), 0)).where(
                Trade.user_id == user.id,
                Trade.side == TradeSide.BUY,
                Trade.status.in_((TradeStatus.SUBMITTED, TradeStatus.SUCCESS)),
                Trade.created_at >= since,
            )
        )
    ).one()
    return int(row[0]), int(row[1])
