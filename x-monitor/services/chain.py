"""Robinhood Chain client: Pons v2 launches (bonding curve) and graduated Uniswap v4 pools.

Venues
* phase 0 (NotGraduated): trade on the token's Pons curve: buy(quoteIn, minTokensOut, recipient) / sell(...)
* phase 2 (PoolCreated):  trade on the Uniswap v4 pool (ETH, token, fee, tickSpacing, Pons meme hook)
  through the Universal Router (V4_SWAP), priced with the official V4Quoter.

Addresses: Pons docs (docs.ponsfamily.com/v2), Uniswap v4 deployments ("Robinhood Chain: 4663") and
Uniswap/universal-router deploy-addresses/robinhood.json.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import ContractLogicError, TransactionNotFound

log = logging.getLogger(__name__)

CHAIN_ID = 4663
NATIVE = "0x0000000000000000000000000000000000000000"
PONS_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
PONS_MEME_HOOK = "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044"
UNIVERSAL_ROUTER = "0x204FAca1764B154221e35c0d20aBb3c525710498"  # UniversalRouterV2_1_2
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
EXPLORER = "https://robinhoodchain.blockscout.com"

PHASE_CURVE = 0
PHASE_POOL = 2
BPS = 10_000
MAX_UINT160 = 2**160 - 1
MAX_UINT256 = 2**256 - 1

# Universal Router / v4-periphery command and action ids
CMD_V4_SWAP = 0x10
ACT_SWAP_EXACT_IN_SINGLE = 0x06
ACT_SETTLE_ALL = 0x0C
ACT_TAKE_ALL = 0x0F


def _fn(name: str, inputs: list[tuple[str, str]], outputs: list[str], mutability: str = "view") -> dict:
    return {
        "name": name,
        "type": "function",
        "stateMutability": mutability,
        "inputs": [{"name": n, "type": t} for n, t in inputs],
        "outputs": [{"name": f"o{i}", "type": t} for i, t in enumerate(outputs)],
    }


FACTORY_ABI = [
    {
        "name": "getLaunchedToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": n, "type": t}
                    for n, t in [
                        ("token", "address"),
                        ("curve", "address"),
                        ("deployer", "address"),
                        ("creatorFeeRecipient", "address"),
                        ("pairToken", "address"),
                        ("graduationThreshold", "uint256"),
                        ("poolFee", "uint24"),
                        ("tickSpacing", "int24"),
                        ("creatorTaxBps", "uint16"),
                        ("buybackEnabled", "bool"),
                        ("phase", "uint8"),
                        ("sweptQuote", "uint256"),
                        ("sweptTokens", "uint256"),
                        ("sweptAt", "uint256"),
                        ("exists", "bool"),
                    ]
                ],
            }
        ],
    }
]
CURVE_ABI = [
    _fn("getReserves", [], ["uint256", "uint256"]),
    _fn("feeBps", [], ["uint256"]),
    _fn("creatorTaxBps", [], ["uint256"]),
    _fn("currentSnipeTaxBps", [("recipient", "address")], ["uint256"]),
    _fn("sellableTokens", [], ["uint256"]),
    _fn("readyToGraduate", [], ["bool"]),
    _fn(
        "buy",
        [("quoteIn", "uint256"), ("minTokensOut", "uint256"), ("recipient", "address")],
        ["uint256"],
        "payable",
    ),
    _fn(
        "sell",
        [("tokensIn", "uint256"), ("minQuoteOut", "uint256"), ("recipient", "address")],
        ["uint256"],
        "nonpayable",
    ),
]
ERC20_ABI = [
    _fn("symbol", [], ["string"]),
    _fn("name", [], ["string"]),
    _fn("decimals", [], ["uint8"]),
    _fn("totalSupply", [], ["uint256"]),
    _fn("balanceOf", [("a", "address")], ["uint256"]),
    _fn("allowance", [("o", "address"), ("s", "address")], ["uint256"]),
    _fn("approve", [("s", "address"), ("v", "uint256")], ["bool"], "nonpayable"),
]
PERMIT2_ABI = [
    _fn("allowance", [("u", "address"), ("t", "address"), ("s", "address")], ["uint160", "uint48", "uint48"]),
    _fn(
        "approve",
        [("token", "address"), ("spender", "address"), ("amount", "uint160"), ("expiration", "uint48")],
        [],
        "nonpayable",
    ),
]
_POOL_KEY = "(address,address,uint24,int24,address)"
QUOTER_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {
                        "name": "poolKey",
                        "type": "tuple",
                        "components": [
                            {"name": "currency0", "type": "address"},
                            {"name": "currency1", "type": "address"},
                            {"name": "fee", "type": "uint24"},
                            {"name": "tickSpacing", "type": "int24"},
                            {"name": "hooks", "type": "address"},
                        ],
                    },
                    {"name": "zeroForOne", "type": "bool"},
                    {"name": "exactAmount", "type": "uint128"},
                    {"name": "hookData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "gasEstimate", "type": "uint256"}],
    }
]
ROUTER_ABI = [
    _fn(
        "execute",
        [("commands", "bytes"), ("inputs", "bytes[]"), ("deadline", "uint256")],
        [],
        "payable",
    )
]


# ---------------------------------------------------------------- data


class ChainError(Exception):
    pass


class NotPonsToken(ChainError):
    pass


@dataclass(slots=True)
class Launch:
    token: str
    curve: str
    pair_token: str
    phase: int
    creator_tax_bps: int
    pool_fee: int
    tick_spacing: int
    symbol: str = ""
    decimals: int = 18
    total_supply: int = 0

    @property
    def native(self) -> bool:
        return self.pair_token.lower() == NATIVE

    @property
    def venue(self) -> str | None:
        return {PHASE_CURVE: "curve", PHASE_POOL: "pool"}.get(self.phase)

    @property
    def pool_key(self) -> tuple:
        # Native ETH is the zero address and therefore always currency0.
        return (NATIVE, self.token, self.pool_fee, self.tick_spacing, PONS_MEME_HOOK)


@dataclass(slots=True)
class Quote:
    amount_in: int
    amount_out: int
    venue: str
    snipe_tax_bps: int = 0

    def mcap_wei(self, total_supply: int) -> int:
        """Market cap implied by this trade's effective price (includes fees and price impact)."""
        if self.amount_out <= 0:
            return MAX_UINT256
        return total_supply * self.amount_in // self.amount_out


@dataclass(slots=True)
class TxResult:
    tx_hash: str
    success: bool
    amount_in: int
    amount_out: int
    gas_cost_wei: int = 0


def wei_to_eth(value: int) -> Decimal:
    return Decimal(value) / Decimal(10**18)


def eth_to_wei(value: Decimal | str | float) -> int:
    return int(Decimal(str(value)) * Decimal(10**18))


def tx_url(tx_hash: str) -> str:
    return f"{EXPLORER}/tx/{tx_hash if tx_hash.startswith('0x') else '0x' + tx_hash}"


def token_url(token: str) -> str:
    return f"https://ponsfamily.com/launchpad/{token}"


# ---------------------------------------------------------------- encoding


def encode_v4_swap_exact_in_single(
    pool_key: tuple, zero_for_one: bool, amount_in: int, min_out: int
) -> tuple[bytes, list[bytes]]:
    """Universal Router payload: V4_SWAP(SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL)."""
    currency_in, currency_out = (pool_key[0], pool_key[1]) if zero_for_one else (pool_key[1], pool_key[0])
    actions = bytes([ACT_SWAP_EXACT_IN_SINGLE, ACT_SETTLE_ALL, ACT_TAKE_ALL])
    params = [
        encode([f"({_POOL_KEY},bool,uint128,uint128,bytes)"], [(pool_key, zero_for_one, amount_in, min_out, b"")]),
        encode(["address", "uint256"], [currency_in, amount_in]),
        encode(["address", "uint256"], [currency_out, min_out]),
    ]
    return bytes([CMD_V4_SWAP]), [encode(["bytes", "bytes[]"], [actions, params])]


def curve_buy_out(
    amount_in: int, quote_reserve: int, token_reserve: int, fee_bps: int, tax_bps: int, snipe_bps: int
) -> int:
    """Pons v2 quoting formula (docs.ponsfamily.com/v2#quoting): fees come off the input."""
    spent = amount_in - amount_in * fee_bps // BPS - amount_in * tax_bps // BPS - amount_in * snipe_bps // BPS
    if spent <= 0:
        return 0
    return spent * token_reserve // (quote_reserve + spent)


def curve_sell_out(tokens_in: int, quote_reserve: int, token_reserve: int, fee_bps: int, tax_bps: int) -> int:
    gross = tokens_in * quote_reserve // (token_reserve + tokens_in)
    return gross - gross * fee_bps // BPS - gross * tax_bps // BPS


def apply_slippage(amount: int, slippage_bps: int) -> int:
    return amount * (BPS - slippage_bps) // BPS


# ---------------------------------------------------------------- client


class ChainClient:
    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        *,
        receipt_timeout: float = 90.0,
        gas_multiplier: float = 1.3,
    ) -> None:
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        self.account: LocalAccount = Account.from_key(private_key)
        self.address = self.account.address
        self._lock = asyncio.Lock()  # one wallet -> serialize nonces
        self._receipt_timeout = receipt_timeout
        self._gas_multiplier = gas_multiplier
        self.factory = self.w3.eth.contract(address=PONS_FACTORY, abi=FACTORY_ABI)
        self.quoter = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(V4_QUOTER), abi=QUOTER_ABI)
        self.router = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(UNIVERSAL_ROUTER), abi=ROUTER_ABI)
        self.permit2 = self.w3.eth.contract(address=PERMIT2, abi=PERMIT2_ABI)

    # ------------------------------------------------------------ reads

    def _erc20(self, token: str):
        return self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(token), abi=ERC20_ABI)

    def _curve(self, curve: str):
        return self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(curve), abi=CURVE_ABI)

    async def chain_id(self) -> int:
        return await self.w3.eth.chain_id

    async def eth_balance(self) -> int:
        return await self.w3.eth.get_balance(self.address)

    async def token_balance(self, token: str) -> int:
        return await self._erc20(token).functions.balanceOf(self.address).call()

    async def get_launch(self, token: str) -> Launch:
        try:
            token = AsyncWeb3.to_checksum_address(token)
        except ValueError as exc:
            raise NotPonsToken(f"invalid address {token}") from exc
        rec = await self.factory.functions.getLaunchedToken(token).call()
        if not rec[14]:
            raise NotPonsToken(f"{token} is not a Pons v2 launch")
        launch = Launch(
            token=token,
            curve=rec[1],
            pair_token=rec[4],
            phase=int(rec[10]),
            creator_tax_bps=int(rec[8]),
            pool_fee=int(rec[6]),
            tick_spacing=int(rec[7]),
        )
        erc = self._erc20(token)
        launch.symbol, launch.decimals, launch.total_supply = await asyncio.gather(
            erc.functions.symbol().call(), erc.functions.decimals().call(), erc.functions.totalSupply().call()
        )
        return launch

    async def quote_buy(self, launch: Launch, amount_in: int) -> Quote:
        if launch.venue == "curve":
            curve = self._curve(launch.curve)
            (q_res, t_res), fee, tax, snipe = await asyncio.gather(
                curve.functions.getReserves().call(),
                curve.functions.feeBps().call(),
                curve.functions.creatorTaxBps().call(),
                curve.functions.currentSnipeTaxBps(self.address).call(),
            )
            out = curve_buy_out(amount_in, q_res, t_res, fee, tax, snipe)
            return Quote(amount_in, out, "curve", snipe_tax_bps=int(snipe))
        if launch.venue == "pool":
            out, _gas = await self.quoter.functions.quoteExactInputSingle(
                (launch.pool_key, True, amount_in, b"")
            ).call()
            return Quote(amount_in, out, "pool")
        raise ChainError(f"launch phase {launch.phase} is not tradable")

    async def quote_sell(self, launch: Launch, tokens_in: int) -> Quote:
        if launch.venue == "curve":
            curve = self._curve(launch.curve)
            (q_res, t_res), fee, tax = await asyncio.gather(
                curve.functions.getReserves().call(),
                curve.functions.feeBps().call(),
                curve.functions.creatorTaxBps().call(),
            )
            return Quote(tokens_in, curve_sell_out(tokens_in, q_res, t_res, fee, tax), "curve")
        if launch.venue == "pool":
            out, _gas = await self.quoter.functions.quoteExactInputSingle(
                (launch.pool_key, False, tokens_in, b"")
            ).call()
            return Quote(tokens_in, out, "pool")
        raise ChainError(f"launch phase {launch.phase} is not tradable")

    # ------------------------------------------------------------ writes

    async def _fees(self) -> dict[str, int]:
        block = await self.w3.eth.get_block("latest")
        base = int(block.get("baseFeePerGas") or await self.w3.eth.gas_price)
        # Robinhood Chain orders by arrival; a priority fee does not buy precedence.
        return {"maxFeePerGas": base * 2 + 1, "maxPriorityFeePerGas": 0}

    async def _send(self, to: str, data: bytes | str, value: int = 0, on_signed=None) -> tuple[str, Any]:
        """Signs and broadcasts; ``on_signed(tx_hash)`` runs before broadcast so the hash can be persisted."""
        async with self._lock:
            tx: dict[str, Any] = {
                "from": self.address,
                "to": AsyncWeb3.to_checksum_address(to),
                "data": data if isinstance(data, str) else "0x" + data.hex(),
                "value": value,
                "chainId": CHAIN_ID,
                "nonce": await self.w3.eth.get_transaction_count(self.address, "pending"),
                "type": 2,
                **(await self._fees()),
            }
            try:
                gas = await self.w3.eth.estimate_gas(tx)
            except ContractLogicError as exc:
                raise ChainError(f"transaction would revert: {exc}") from exc
            tx["gas"] = int(gas * self._gas_multiplier)
            signed = self.account.sign_transaction(tx)
            tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
            if on_signed is not None:
                await on_signed(tx_hash)
            await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await self.wait_receipt(tx_hash)
        return tx_hash, receipt

    async def wait_receipt(self, tx_hash: str, timeout: float | None = None) -> Any:
        deadline = time.monotonic() + (timeout or self._receipt_timeout)
        while True:
            try:
                return await self.w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                if time.monotonic() > deadline:
                    raise ChainError(f"no receipt for {tx_hash} after timeout") from None
                await asyncio.sleep(0.5)

    async def get_receipt(self, tx_hash: str) -> Any | None:
        try:
            return await self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None

    @staticmethod
    def _gas_cost(receipt: Any) -> int:
        return int(receipt.get("gasUsed", 0)) * int(receipt.get("effectiveGasPrice", 0))

    async def _ensure_allowance(self, token: str, spender: str, amount: int) -> None:
        erc = self._erc20(token)
        if await erc.functions.allowance(self.address, spender).call() >= amount:
            return
        data = erc.encode_abi("approve", [AsyncWeb3.to_checksum_address(spender), MAX_UINT256])
        tx_hash, receipt = await self._send(token, data)
        if receipt["status"] != 1:
            raise ChainError(f"approve failed: {tx_hash}")

    async def _ensure_permit2(self, token: str, amount: int) -> None:
        await self._ensure_allowance(token, PERMIT2, amount)
        router = AsyncWeb3.to_checksum_address(UNIVERSAL_ROUTER)
        allowed, expiration, _nonce = await self.permit2.functions.allowance(self.address, token, router).call()
        if allowed >= amount and expiration > time.time() + 60:
            return
        data = self.permit2.encode_abi("approve", [token, router, MAX_UINT160, 2**48 - 1])
        tx_hash, receipt = await self._send(PERMIT2, data)
        if receipt["status"] != 1:
            raise ChainError(f"permit2 approve failed: {tx_hash}")

    async def buy(self, launch: Launch, amount_in: int, min_out: int, on_signed=None) -> TxResult:
        if not launch.native:
            raise ChainError("only ETH-paired launches are supported")
        before = await self.token_balance(launch.token)
        if launch.venue == "curve":
            data = self._curve(launch.curve).encode_abi("buy", [amount_in, min_out, self.address])
            tx_hash, receipt = await self._send(launch.curve, data, value=amount_in, on_signed=on_signed)
        elif launch.venue == "pool":
            commands, inputs = encode_v4_swap_exact_in_single(launch.pool_key, True, amount_in, min_out)
            data = self.router.encode_abi("execute", [commands, inputs, int(time.time()) + 300])
            tx_hash, receipt = await self._send(UNIVERSAL_ROUTER, data, value=amount_in, on_signed=on_signed)
        else:
            raise ChainError(f"launch phase {launch.phase} is not tradable")
        ok = receipt["status"] == 1
        after = await self.token_balance(launch.token) if ok else before
        return TxResult(tx_hash, ok, amount_in, after - before, self._gas_cost(receipt))

    async def sell(self, launch: Launch, tokens_in: int, min_out: int, on_signed=None) -> TxResult:
        if not launch.native:
            raise ChainError("only ETH-paired launches are supported")
        if launch.venue == "curve":
            await self._ensure_allowance(launch.token, launch.curve, tokens_in)
            before = await self.eth_balance()
            data = self._curve(launch.curve).encode_abi("sell", [tokens_in, min_out, self.address])
            tx_hash, receipt = await self._send(launch.curve, data, on_signed=on_signed)
        elif launch.venue == "pool":
            await self._ensure_permit2(launch.token, tokens_in)
            before = await self.eth_balance()
            commands, inputs = encode_v4_swap_exact_in_single(launch.pool_key, False, tokens_in, min_out)
            data = self.router.encode_abi("execute", [commands, inputs, int(time.time()) + 300])
            tx_hash, receipt = await self._send(UNIVERSAL_ROUTER, data, on_signed=on_signed)
        else:
            raise ChainError(f"launch phase {launch.phase} is not tradable")
        ok = receipt["status"] == 1
        gas = self._gas_cost(receipt)
        received = (await self.eth_balance()) - before + gas if ok else 0
        return TxResult(tx_hash, ok, tokens_in, received, gas)
