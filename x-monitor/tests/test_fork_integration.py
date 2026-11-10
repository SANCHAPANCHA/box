"""TradeExecutor + real ChainClient against an anvil fork of Robinhood Chain (skipped unless FORK_RPC is set).

anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8546 --chain-id 4663
FORK_RPC=http://127.0.0.1:8546 FORK_CURVE_TOKEN=0x… FORK_POOL_TOKEN=0x… pytest tests/test_fork_integration.py
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from eth_account import Account
from pydantic import SecretStr
from sqlalchemy import select

from core.database import session_scope
from models import Trade, TradeSide, TradeStatus, User
from services.chain import ChainClient
from services.trading import TradeExecutor, queue_sell
from tests.conftest import make_user

FORK_RPC = os.environ.get("FORK_RPC")
TOKENS = [t for t in (os.environ.get("FORK_CURVE_TOKEN"), os.environ.get("FORK_POOL_TOKEN")) if t]

pytestmark = pytest.mark.skipif(not (FORK_RPC and TOKENS), reason="needs FORK_RPC and FORK_*_TOKEN")


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []

    async def __call__(self, chat_id: int, text: str, sell_trade_id: int | None) -> None:
        self.sent.append((chat_id, text, sell_trade_id))


@pytest.mark.parametrize("token", TOKENS)
async def test_buy_and_sell_on_fork(sf, settings, token) -> None:
    account = Account.create()  # fresh key: anvil's dev keys have sweeper code on the real chain
    chain = ChainClient(FORK_RPC, account.key.hex())
    await chain.w3.provider.make_request("anvil_setBalance", [account.address, hex(10**18)])
    cfg = settings.model_copy(update={"trading_enabled": True, "wallet_private_key": SecretStr(account.key.hex())})
    outbox = Outbox()
    executor = TradeExecutor(sf, chain, cfg, outbox)
    user = await make_user(sf, autobuy_enabled=True, buy_amount_eth=Decimal("0.003"))

    async with session_scope(sf) as session:
        session.add(
            Trade(
                user_id=user.id,
                chat_id=user.chat_id,
                side=TradeSide.BUY,
                token_address=token.lower(),
                amount_in=3 * 10**15,
            )  # fmt: skip
        )
    assert await executor.run_once() == 1
    async with sf() as session:
        buy = await session.scalar(select(Trade))
    assert buy.status is TradeStatus.SUCCESS, (buy.reason, outbox.sent)
    assert int(buy.amount_out) == await chain.token_balance(token) > 0
    assert "Куплен токен" in outbox.sent[-1][1]

    async with session_scope(sf) as session:
        await queue_sell(session, await session.get(User, user.id), buy.id, 100)
    eth_before = await chain.eth_balance()
    assert await executor.run_once() == 1
    async with sf() as session:
        sell = await session.scalar(select(Trade).where(Trade.side == TradeSide.SELL))
    assert sell.status is TradeStatus.SUCCESS, (sell.reason, outbox.sent)
    assert await chain.token_balance(token) == 0
    assert int(sell.amount_out) > 0 and await chain.eth_balance() > eth_before - 10**15
    assert "Продано 100%" in outbox.sent[-1][1]
