"""Manual fork check: real buy/sell against an anvil fork of Robinhood Chain.

anvil --fork-url https://rpc.mainnet.chain.robinhood.com --port 8546 --chain-id 4663
python -m tests.fork.check_fork 0xCURVE_TOKEN 0xPOOL_TOKEN
"""

from __future__ import annotations

import asyncio
import os
import sys

from eth_account import Account

from services.chain import ChainClient, apply_slippage, eth_to_wei, wei_to_eth


async def roundtrip(chain: ChainClient, token: str) -> None:
    launch = await chain.get_launch(token)
    units = 10**launch.decimals
    print(f"\n== {launch.symbol} venue={launch.venue} tax={launch.creator_tax_bps}bps")
    amount = eth_to_wei("0.01")
    quote = await chain.quote_buy(launch, amount)
    mcap = wei_to_eth(quote.mcap_wei(launch.total_supply))
    print(f"quote: {quote.amount_out / units:,.0f} tokens, mcap≈{mcap:.3f} ETH")
    buy = await chain.buy(launch, amount, apply_slippage(quote.amount_out, 500))
    print(f"buy  ok={buy.success} got={buy.amount_out / units:,.0f} gas={wei_to_eth(buy.gas_cost_wei)}")
    print(f"     tx={buy.tx_hash}")
    assert buy.success and buy.amount_out >= apply_slippage(quote.amount_out, 500)

    half = buy.amount_out // 2
    squote = await chain.quote_sell(launch, half)
    sell = await chain.sell(launch, half, apply_slippage(squote.amount_out, 500))
    print(f"sell ok={sell.success} eth={wei_to_eth(sell.amount_out)} (quote {wei_to_eth(squote.amount_out)})")
    assert sell.success and sell.amount_out > 0
    rest = await chain.token_balance(token)
    sell2 = await chain.sell(launch, rest, 0)
    print(f"sell rest ok={sell2.success} eth={wei_to_eth(sell2.amount_out)} left={await chain.token_balance(token)}")
    assert sell2.success


async def main() -> None:
    # Fresh key: anvil's well-known dev accounts carry EIP-7702 sweeper code on the real chain.
    account = Account.create()
    chain = ChainClient(os.environ.get("FORK_RPC", "http://127.0.0.1:8546"), account.key.hex())
    await chain.w3.provider.make_request("anvil_setBalance", [account.address, hex(10**19)])
    print("chain", await chain.chain_id(), "balance", wei_to_eth(await chain.eth_balance()))
    for token in sys.argv[1:]:
        await roundtrip(chain, token)
    print("\nFORK CHECK PASSED")


if __name__ == "__main__":
    asyncio.run(main())
