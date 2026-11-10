"""Auto-buy pipeline: planner (inside Monitor), executor limits, sells, restart recovery, bot menu."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from pydantic import SecretStr
from sqlalchemy import select, update

from bot.callbacks import Nav, Trd
from core.database import session_scope
from models import EventType, Subscription, Trade, TradeSide, TradeStatus, User
from services.account_manager import AccountManager
from services.alerts import Alerter
from services.cache import Cache
from services.chain import ChainError, Launch, NotPonsToken, Quote, TxResult, eth_to_wei
from services.monitor import Monitor
from services.trading import TradeExecutor, TradePlanner, extract_contracts
from tests.conftest import AlertRecorder, FakeX, future_id, make_user, post
from tests.test_monitor import _make_due
from tests.tg_helpers import OWNER, callback_update, message_update, texts

UID = 44196397
TOKEN = "0x8C214B77f7eDeDa453EBD43Df848Cc02ecc1b431"
OTHER = "0x00000000000000000000000000000000DeaDBeef"
SUPPLY = 10**27


class FakeChain:
    address = "0x1111111111111111111111111111111111111111"

    def __init__(self) -> None:
        self.launches: dict[str, Launch] = {
            TOKEN.lower(): Launch(TOKEN, "0xcurve", "0x" + "0" * 40, 0, 200, 0, 200, "LGND", 18, SUPPLY)
        }
        self.balance = eth_to_wei("1")
        self.tokens: dict[str, int] = {}
        self.price_tokens_per_eth = 500_000  # -> mcap 2000 ETH for 1e9 supply
        self.snipe_bps = 0
        self.buys: list[tuple[str, int, int]] = []
        self.sells: list[tuple[str, int, int]] = []
        self.revert = False
        self.receipts: dict[str, dict] = {}
        self.fail: Exception | None = None

    async def get_launch(self, token: str) -> Launch:
        launch = self.launches.get(token.lower())
        if launch is None:
            raise NotPonsToken(token)
        return launch

    async def quote_buy(self, launch: Launch, amount_in: int) -> Quote:
        return Quote(amount_in, amount_in * self.price_tokens_per_eth, launch.venue, self.snipe_bps)

    async def quote_sell(self, launch: Launch, tokens_in: int) -> Quote:
        return Quote(tokens_in, tokens_in // self.price_tokens_per_eth, launch.venue)

    async def eth_balance(self) -> int:
        return self.balance

    async def token_balance(self, token: str) -> int:
        return self.tokens.get(token.lower(), 0)

    async def get_receipt(self, tx_hash: str):
        return self.receipts.get(tx_hash)

    async def buy(self, launch: Launch, amount_in: int, min_out: int, on_signed=None) -> TxResult:
        if self.fail:
            raise self.fail
        tx = f"0x{len(self.buys) + 1:064x}"
        if on_signed:
            await on_signed(tx)
        self.buys.append((launch.token, amount_in, min_out))
        if self.revert:
            return TxResult(tx, False, amount_in, 0, 10)
        out = amount_in * self.price_tokens_per_eth
        self.balance -= amount_in
        self.tokens[launch.token.lower()] = self.tokens.get(launch.token.lower(), 0) + out
        return TxResult(tx, True, amount_in, out, 10)

    async def sell(self, launch: Launch, tokens_in: int, min_out: int, on_signed=None) -> TxResult:
        tx = f"0x{len(self.sells) + 1000:064x}"
        if on_signed:
            await on_signed(tx)
        self.sells.append((launch.token, tokens_in, min_out))
        self.tokens[launch.token.lower()] -= tokens_in
        return TxResult(tx, True, tokens_in, tokens_in // self.price_tokens_per_eth, 10)


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []

    async def __call__(self, chat_id: int, text: str, sell_trade_id: int | None) -> None:
        self.sent.append((chat_id, text, sell_trade_id))


@pytest.fixture
def trading_settings(settings):
    return settings.model_copy(update={"trading_enabled": True, "wallet_private_key": SecretStr("0x" + "ab" * 32)})


async def _setup(sf, settings, **user_kw):
    x = FakeX()
    x.add_user("elonmusk", UID)
    user = await make_user(sf, autobuy_enabled=True, **user_kw)
    async with session_scope(sf) as session:
        await AccountManager(x, Cache()).add(session, user, "@elonmusk")
    monitor = Monitor(sf, x, Alerter(sf, AlertRecorder()), settings, TradePlanner(settings))
    chain, outbox = FakeChain(), Outbox()
    executor = TradeExecutor(sf, chain, settings, outbox)
    return x, user, monitor, chain, outbox, executor


async def _trades(sf) -> list[Trade]:
    async with sf() as session:
        return list(await session.scalars(select(Trade).order_by(Trade.id)))


def test_extract_contracts() -> None:
    text = f"New launch! $CA:{TOKEN} also see https://ponsfamily.com/launchpad/{TOKEN.lower()} and {OTHER}"
    assert extract_contracts(text) == [TOKEN, OTHER]
    assert extract_contracts("0x123 nothing here 0x" + "g" * 40) == []
    assert extract_contracts("tx 0x" + "a" * 64) == []  # a tx hash is not an address


async def test_own_post_with_contract_is_bought_once(sf, trading_settings) -> None:
    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings)
    x.posts[UID] = [
        post(future_id(1), text_=f"gm $CA:{TOKEN}"),
        post(future_id(2), EventType.REPLY, text_=f"reply {OTHER}"),  # replies never trigger buys
        post(future_id(3), EventType.QUOTE, text_=f"quote {OTHER}"),
        post(future_id(4), text_=f"again {TOKEN.lower()}"),  # same token -> no second buy
    ]
    await monitor.run_once()
    trades = await _trades(sf)
    assert [(t.side, t.token_address, t.status) for t in trades] == [
        (TradeSide.BUY, TOKEN.lower(), TradeStatus.PENDING)
    ]
    assert int(trades[0].amount_in) == eth_to_wei("0.01")

    assert await executor.run_once() == 1
    trade = (await _trades(sf))[0]
    assert trade.status is TradeStatus.SUCCESS and trade.token_symbol == "LGND" and trade.venue == "curve"
    assert int(trade.amount_out) == eth_to_wei("0.01") * chain.price_tokens_per_eth
    assert trade.tx_hash and int(trade.gas_wei) == 10
    token, amount, min_out = chain.buys[0]
    assert amount == eth_to_wei("0.01") and min_out == amount * chain.price_tokens_per_eth * 9 // 10  # 10% slippage
    chat_id, text, sell_id = outbox.sent[-1]
    assert chat_id == user.chat_id and "Куплен токен LGND" in text and sell_id == trade.id
    assert await executor.run_once() == 0


async def test_no_buy_when_disabled(sf, trading_settings, settings) -> None:
    x, user, monitor, *_ = await _setup(sf, trading_settings)
    async with session_scope(sf) as session:
        await session.execute(update(Subscription).values(autobuy_enabled=False))
    x.posts[UID] = [post(future_id(1), text_=TOKEN)]
    await monitor.run_once()
    assert await _trades(sf) == []

    # Account switch back on, but trading is not configured on the server -> never planned
    async with session_scope(sf) as session:
        await session.execute(update(Subscription).values(autobuy_enabled=True))
    not_configured = Monitor(sf, x, Alerter(sf, AlertRecorder()), settings, TradePlanner(settings))
    x.posts[UID].append(post(future_id(2), text_=TOKEN))
    await _make_due(sf)
    await not_configured.run_once()
    assert await _trades(sf) == []


@pytest.mark.parametrize(
    ("user_kw", "chain_patch", "reason"),
    [
        ({"max_mcap_eth": Decimal("100")}, {}, "капитализация"),
        ({"max_creator_tax_bps": 100}, {}, "налог создателя"),
        ({"daily_limit_eth": Decimal("0.005")}, {}, "дневной лимит"),
        ({}, {"balance": eth_to_wei("0.005")}, "недостаточно ETH"),
        ({}, {"pair": "0x" + "1" * 40}, "не за ETH"),
        ({}, {"phase": 1}, "переходном состоянии"),
    ],
)
async def test_limits_skip_and_notify(sf, trading_settings, user_kw, chain_patch, reason) -> None:
    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings, **user_kw)
    if "balance" in chain_patch:
        chain.balance = chain_patch["balance"]
    if "pair" in chain_patch or "phase" in chain_patch:
        chain.launches[TOKEN.lower()] = replace(
            chain.launches[TOKEN.lower()],
            pair_token=chain_patch.get("pair", "0x" + "0" * 40),
            phase=chain_patch.get("phase", 0),
        )
    x.posts[UID] = [post(future_id(1), text_=f"$CA:{TOKEN}")]
    await monitor.run_once()
    await executor.run_once()
    trade = (await _trades(sf))[0]
    assert trade.status is TradeStatus.SKIPPED and reason in trade.reason
    assert chain.buys == []
    assert "Автопокупка пропущена" in outbox.sent[-1][1] and reason in outbox.sent[-1][1]


async def test_daily_count_limit_and_non_pons_address(sf, trading_settings) -> None:
    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings, max_buys_per_day=1)
    second = "0x2222222222222222222222222222222222222222"
    chain.launches[second] = replace(chain.launches[TOKEN.lower()], token=second, symbol="TWO")
    x.posts[UID] = [post(future_id(1), text_=f"{OTHER} {TOKEN}"), post(future_id(2), text_=second)]
    await monitor.run_once()
    await executor.run_once()
    by_token = {t.token_address: t for t in await _trades(sf)}
    assert by_token[OTHER.lower()].status is TradeStatus.SKIPPED  # not a Pons token: silent
    assert by_token[TOKEN.lower()].status is TradeStatus.SUCCESS
    assert by_token[second].status is TradeStatus.SKIPPED and "лимит покупок" in by_token[second].reason
    assert not any("DeaDBeef".lower() in text.lower() for _, text, _ in outbox.sent)


async def test_stale_snipe_revert_and_errors(sf, trading_settings) -> None:
    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings)
    x.posts[UID] = [post(future_id(1), text_=TOKEN)]
    await monitor.run_once()
    async with session_scope(sf) as session:
        await session.execute(update(Trade).values(created_at=datetime.now(UTC) - timedelta(hours=1)))
    await executor.run_once()
    assert (await _trades(sf))[0].status is TradeStatus.SKIPPED and chain.buys == []

    async with session_scope(sf) as session:
        await session.execute(
            update(Trade).values(status=TradeStatus.PENDING, created_at=datetime.now(UTC), reason=None)
        )
    chain.revert = True
    await executor.run_once()
    trade = (await _trades(sf))[0]
    assert trade.status is TradeStatus.FAILED and trade.tx_hash
    assert "Покупка не прошла" in outbox.sent[-1][1]

    async with session_scope(sf) as session:
        await session.execute(update(Trade).values(status=TradeStatus.PENDING, tx_hash=None))
    chain.revert, chain.fail = False, ChainError("transaction would revert: slippage")
    await executor.run_once()
    trade = (await _trades(sf))[0]
    assert trade.status is TradeStatus.FAILED and "slippage" in trade.reason


async def test_sell_flow(sf, trading_settings) -> None:
    from services.trading import queue_sell

    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings)
    x.posts[UID] = [post(future_id(1), text_=TOKEN)]
    await monitor.run_once()
    await executor.run_once()
    buy = (await _trades(sf))[0]
    bought = chain.tokens[TOKEN.lower()]

    async with session_scope(sf) as session:
        db_user = await session.get(User, user.id)
        assert await queue_sell(session, db_user, buy.id, 33) is None  # only 25/50/100
        assert await queue_sell(session, db_user, buy.id, 50) is not None
    await executor.run_once()
    sell = (await _trades(sf))[1]
    assert sell.status is TradeStatus.SUCCESS and int(sell.amount_in) == bought // 2
    assert chain.sells[0][1] == bought // 2
    assert "Продано 50%" in outbox.sent[-1][1] and outbox.sent[-1][2] == buy.id

    async with session_scope(sf) as session:
        await queue_sell(session, await session.get(User, user.id), buy.id, 100)
    await executor.run_once()
    assert chain.tokens[TOKEN.lower()] == 0
    assert outbox.sent[-1][2] is None  # nothing left -> no more sell buttons

    async with session_scope(sf) as session:
        await queue_sell(session, await session.get(User, user.id), buy.id, 100)
    await executor.run_once()
    assert (await _trades(sf))[-1].status is TradeStatus.FAILED
    assert "нет этого токена" in outbox.sent[-1][1]


async def test_recover_after_crash(sf, trading_settings) -> None:
    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings)
    x.posts[UID] = [post(future_id(1), text_=TOKEN)]
    await monitor.run_once()
    async with session_scope(sf) as session:
        trade = await session.scalar(select(Trade))
        trade.status = TradeStatus.PROCESSING  # crashed before signing
    await executor.recover()
    assert (await _trades(sf))[0].status is TradeStatus.PENDING

    for receipt, expected in [
        ({"status": 1}, TradeStatus.SUCCESS),
        ({"status": 0}, TradeStatus.FAILED),
        (None, TradeStatus.FAILED),
    ]:
        async with session_scope(sf) as session:
            await session.execute(update(Trade).values(status=TradeStatus.SUBMITTED, tx_hash="0xabc"))
        chain.receipts = {"0xabc": receipt} if receipt else {}
        await executor.recover()
        assert (await _trades(sf))[0].status is expected
    assert chain.buys == []  # recovery never re-sends a transaction


async def test_rpc_errors_retry_then_fail(sf, trading_settings) -> None:
    import aiohttp

    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings)
    x.posts[UID] = [post(future_id(1), text_=TOKEN)]
    await monitor.run_once()

    real_balance = chain.eth_balance
    failures = {"left": 2}

    async def flaky_balance() -> int:
        if failures["left"]:
            failures["left"] -= 1
            raise aiohttp.ClientConnectionError("node down")
        return await real_balance()

    chain.eth_balance = flaky_balance
    await executor.run_once()
    await executor.run_once()
    assert (await _trades(sf))[0].status is TradeStatus.PENDING  # retried, nothing signed
    await executor.run_once()
    assert (await _trades(sf))[0].status is TradeStatus.SUCCESS and len(chain.buys) == 1

    # Node permanently down -> fails with a clear message after the retries
    async with session_scope(sf) as session:
        await session.execute(update(Trade).values(status=TradeStatus.PENDING, tx_hash=None))
    failures["left"] = 99
    for _ in range(4):
        await executor.run_once()
    trade = (await _trades(sf))[0]
    assert trade.status is TradeStatus.FAILED and "недоступна" in trade.reason
    assert len(chain.buys) == 1


async def test_stuck_submitted_is_resolved_periodically(sf, trading_settings) -> None:
    x, user, monitor, chain, outbox, executor = await _setup(sf, trading_settings)
    x.posts[UID] = [post(future_id(1), text_=TOKEN)]
    await monitor.run_once()
    async with session_scope(sf) as session:
        await session.execute(
            update(Trade).values(
                status=TradeStatus.SUBMITTED, tx_hash="0xfeed", updated_at=datetime.now(UTC) - timedelta(minutes=5)
            )
        )
    chain.receipts = {"0xfeed": {"status": 1}}
    await executor.run_once()
    assert (await _trades(sf))[0].status is TradeStatus.SUCCESS
    assert "подтверждена позже" in outbox.sent[-1][1] and outbox.sent[-1][2] is not None
    assert chain.buys == []


# ---------------------------------------------------------------- bot menu


async def test_trading_menu(bot_env, sf, trading_settings, settings) -> None:
    bot, dp, session = bot_env
    chain = FakeChain()
    await make_user(sf, telegram_id=OWNER)

    async def cb(data, cfg=trading_settings):
        start = len(session.requests)
        await dp.feed_update(bot, callback_update(data), settings=cfg, chain=chain, account_manager=None)
        return session.requests[start:]

    async def msg(text):
        start = len(session.requests)
        await dp.feed_update(bot, message_update(text), settings=trading_settings, chain=chain, account_manager=None)
        return session.requests[start:]

    out = await cb(Nav(to="trading"), settings)  # not configured on the server
    assert "Автопокупка не настроена" in texts(out)

    out = await cb(Nav(to="trading"))
    assert "Автопокупка токенов Pons" in texts(out) and "ВЫКЛЮЧЕНА" in texts(out) and "1 ETH" in texts(out)
    await cb(Trd(action="toggle"))
    await cb(Trd(action="set", value="amount=0.05"))
    await cb(Trd(action="set", value="mcap=none"))
    await cb(Trd(action="set", value="slip=15"))
    await cb(Trd(action="input", value="daily"))
    out = await msg("0,25")
    assert "Дневной лимит: <b>0.25 ETH" in texts(out)
    await cb(Trd(action="input", value="count"))
    out = await msg("abc")
    assert "Не удалось распознать" in texts(out)
    await msg("нет")
    out = await cb(Trd(action="set", value="amount=-1"))
    assert any(isinstance(r, AnswerCallbackQuery) and r.show_alert for r in out)

    async with sf() as s:
        user = await s.scalar(select(User).where(User.telegram_id == OWNER))
    assert user.autobuy_enabled and user.buy_amount_eth == Decimal("0.05") and user.max_mcap_eth is None
    assert user.slippage_bps == 1500 and user.daily_limit_eth == Decimal("0.25") and user.max_buys_per_day is None

    for data in [
        Trd(action="accounts"),
        Trd(action="positions"),
        Trd(action="history"),
        Trd(action="field", value="tax"),
    ]:
        out = await cb(data)
        assert [r for r in out if isinstance(r, EditMessageText)], data

    # Sell confirmation from a position
    async with session_scope(sf) as s:
        buy = Trade(
            user_id=user.id, chat_id=OWNER, side=TradeSide.BUY, status=TradeStatus.SUCCESS,
            token_address=TOKEN.lower(), token_symbol="LGND", amount_in=eth_to_wei("0.01"),
        )  # fmt: skip
        s.add(buy)
    chain.tokens[TOKEN.lower()] = 10**24
    out = await cb(Trd(action="positions"))
    assert "LGND" in texts(out) and "Trd" not in texts(out)
    out = await cb(Trd(action="sell_ask", id=buy.id, value="50"))
    assert "Продать 50% LGND?" in texts(out)
    out = await cb(Trd(action="sell_yes", id=buy.id, value="50"))
    assert "отправлена" in texts(out)
    async with sf() as s:
        sell = await s.scalar(select(Trade).where(Trade.side == TradeSide.SELL))
    assert sell.status is TradeStatus.PENDING and sell.sell_pct == 50 and sell.parent_id == buy.id
    assert not any(isinstance(r, SendMessage) and "Traceback" in r.text for r in session.requests)
