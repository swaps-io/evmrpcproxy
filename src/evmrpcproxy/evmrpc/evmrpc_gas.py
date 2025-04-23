from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypedDict

from hyapp.https import HTTPClient

if TYPE_CHECKING:
    from decimal import Decimal

LOGGER = logging.getLogger(__name__)

# Handler for calling the upstream node.
#   * [{method, params}, ...] -> [result_value, ...]
#   * Handler should handle the `jsonrpc` and `id` params.
#   * The errors should be raised as `GasError(data)`
#     * Except for `MethodUnavailable` (`"code":32601` / `"code":-32601`)
TSimpleEVMRPCHandler = Callable[[list], Awaitable[Any]]

# Partial equivalent of `web3.types.TxParams`
TxParamsSimple = TypedDict(
    "TxParamsSimple",
    {
        "chainId": str,
        "data": bytes | str,
        "from": str,
        "gas": str,
        # legacy pricing
        "gasPrice": str,
        # dynamic fee pricing
        "maxFeePerGas": str,
        "maxPriorityFeePerGas": str,
        # addr or ens
        "to": str,
        "value": str,
    },
    total=False,
)


def normalize_tx_params(data: dict) -> TxParamsSimple:
    result = {**data}

    for key in ("gas", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas"):
        # For now, ignore (drop) these.
        result.pop(key, None)

    for key in ("value", "chainId"):
        val = result.get(key)
        if isinstance(val, int):
            result[key] = hex(val)
        if isinstance(val, str) and val.isdigit():
            result[key] = hex(int(val))

    return TxParamsSimple(**result)


EXAMPLE_TX_REQ = TxParamsSimple(
    {
        "chainId": "0x1",
        "data": "0x82ad56cb00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000000",
        "from": "0x29097A7dc18F1d7B736Ead6328370913AB8d845c",
        "value": "0x0",
        "to": "0xcA11bde05977b3631167028862bE2a173976CA11",
        "maxPriorityFeePerGas": "0x23f910",
        "maxFeePerGas": "0x26cc148a3",
    }
)
EXAMPLE_TX_RESP = TxParamsSimple(
    {
        "maxPriorityFeePerGas": "0x1259298f",
        "maxFeePerGas": "0x14042d67",
        "gas": "0xada2",
    }
)


class MethodUnavailableSimple(Exception):
    """Simplified equivalent of web3.exceptions.MethodUnavailable"""


class GasError(Exception):
    """
    An error to be returned to the EVMRPC requester.
    The first exception argument should end up in the `error` key of the response.
    """


def add_pct(value: int, extra_pct: float, frac_mult: int = 10_000) -> int:
    """
    >>> add_pct(1234, 10)
    1357
    >>> int(1234 * 1.1)
    1357
    >>> add_pct(1234, -10)
    1110
    >>> int(1234 * 0.9)
    1110
    """
    extra_frac = int(extra_pct * frac_mult // 100)
    return value * (frac_mult + extra_frac) // frac_mult


def add_pct_hex(value: str, extra_pct: float, frac_mult: int = 10_000) -> str:
    value_int = int(value, 16)
    result_int = add_pct(value_int, extra_pct=extra_pct, frac_mult=frac_mult)
    return hex(result_int)


def gwei_to_wei(value: float | Decimal) -> int:
    # See also: `web3.Web3().to_wei(value, 'gwei')`
    return int(value * 10**9)


async def _build_gas_price_dynamic(req_node: TSimpleEVMRPCHandler) -> TxParamsSimple:
    """
    Build EIP-1559-format gas price parameters.

    Known to raise `web3.exceptions.MethodUnavailable` on unsupported chains.

    Reference: `web3.contract.utils.async_build_transaction_for_function`

    See also:
    https://docs.alchemy.com/docs/maxpriorityfeepergas-vs-maxfeepergas
    """
    # See also: web3._utils.async_transactions.TRANSACTION_DEFAULTS["maxPriorityFeePerGas"]
    reqs = [
        {"method": "eth_maxPriorityFeePerGas", "params": []},  # w3.eth.max_priority_fee
        {"method": "eth_getBlockByNumber", "params": ["latest", False]},  # w3.eth.get_block("latest")
    ]
    resps = await req_node(reqs)
    max_priority_fee_resp, block_resp = resps

    max_priority_fee = int(max_priority_fee_resp, 16)
    base_fee = int(block_resp["baseFeePerGas"], 16)

    max_fee_per_gas = int(max_priority_fee) + (2 * base_fee)
    return TxParamsSimple(maxPriorityFeePerGas=hex(max_priority_fee), maxFeePerGas=hex(max_fee_per_gas))


async def _build_gas_price_legacy(req_node: TSimpleEVMRPCHandler) -> TxParamsSimple:
    """Build pre-EIP-1559 gas price parameter, using `eth_gasPrice`"""
    reqs = [{"method": "eth_gasPrice", "params": []}]
    resps = await req_node(reqs)
    [gas_price_resp] = resps
    gas_price = int(gas_price_resp, 16)
    return TxParamsSimple(gasPrice=hex(gas_price))


async def _build_gas_params_linea(tx_params: TxParamsSimple, req_node: TSimpleEVMRPCHandler) -> TxParamsSimple:
    if not tx_params.get("from"):
        raise GasError({"message": "Tx params need specified `from` for linea"})

    # Note that specifying the block (`[tx_params, "latest"]`) might result in error responses.
    reqs = [{"method": "linea_estimateGas", "params": [tx_params]}]
    resps = await req_node(reqs)
    [resp] = resps
    gas_limit = int(resp["gasLimit"], 16)
    base_fee_per_gas = int(resp["baseFeePerGas"], 16)
    priority_fee_per_gas = int(resp["priorityFeePerGas"], 16)
    return TxParamsSimple(
        maxPriorityFeePerGas=hex(priority_fee_per_gas),
        maxFeePerGas=hex(priority_fee_per_gas + 2 * base_fee_per_gas),
        gas=hex(gas_limit),
    )


def _add_extra_gas_price_and_units(
    tx_params: TxParamsSimple,
    *,
    gas_price_extra_pct: float,
    gas_priority_fee_extra_pct: float,
    gas_units_extra_pct: float,
) -> TxParamsSimple:
    tx_params = tx_params.copy()

    if tx_params.get("gasPrice") and gas_price_extra_pct:
        tx_params["gasPrice"] = add_pct_hex(tx_params.get("gasPrice", "0x0"), gas_price_extra_pct)

    if tx_params.get("maxFeePerGas") and gas_price_extra_pct:
        tx_params["maxFeePerGas"] = add_pct_hex(tx_params.get("maxFeePerGas", "0x0"), gas_price_extra_pct)

    # The `maxPriorityFeePerGas` extra isn't necessarily useful, but should be harmless.
    if tx_params.get("maxPriorityFeePerGas") and gas_priority_fee_extra_pct:
        tx_params["maxPriorityFeePerGas"] = add_pct_hex(
            tx_params.get("maxPriorityFeePerGas", "0x0"), gas_priority_fee_extra_pct
        )

    if tx_params.get("gas") and gas_units_extra_pct:
        tx_params["gas"] = add_pct_hex(tx_params.get("gas", "0x0"), gas_units_extra_pct)

    return tx_params


async def _build_gas_units(tx_params: TxParamsSimple, req_node: TSimpleEVMRPCHandler) -> TxParamsSimple:
    reqs = [{"method": "eth_estimateGas", "params": [tx_params, "latest"]}]
    resps = await req_node(reqs)
    [resp] = resps
    gas_units = int(resp, 16)
    return TxParamsSimple(gas=hex(gas_units))


PRE_EIP1559_CHAIN_IDS: set[int] = {
    # rootstock
    # https://medium.com/iovlabs-innovation-stories/flaws-in-ethereums-eip-1559-c0f91838ce23
    30,
    # polygonZkEvm
    # has no `block["baseFeePerGas"]`
    # Recommends a gasstation API.
    # https://docs.polygon.technology/tools/gas/polygon-gas-station/?h=zkevm#mainnet
    1101,
    # merlin
    # https://docs.merlinchain.io/merlin-docs/developers/builder-guides/fees/fee-model
    4200,
}


POLYGON_GASSTATION_URL = "https://gasstation.polygon.technology/v2"
POLYGONZKEVM_GASSTATION_URL = "https://gasstation.polygon.technology/zkevm"
# Very simple in-memory cache (from a limited amount of keys)
# url -> (timestamp, response_data)
GASSTATION_CACHE: dict[str, tuple[float, Any]] = {}


@dataclasses.dataclass(frozen=True, kw_only=True)
class W3GasHelper:
    chain_id: int
    req_node: TSimpleEVMRPCHandler
    logger: logging.Logger = LOGGER
    http_cli: HTTPClient = dataclasses.field(default_factory=HTTPClient)

    gasstation_key: str = "fast"
    gasstation_cache_ttl_sec: float = 2.0
    gas_price_extra_pct: float = 20
    gas_priority_fee_extra_pct: float = 10
    gas_units_extra_pct: float = 100

    async def _request_gasstation_full(self, url: str) -> dict:
        resp = await self.http_cli.req(url)
        self.logger.debug("Gasstation response from %r: %r", url, resp.content.decode("utf-8", errors="replace"))
        rd = resp.json_untyped()
        if not isinstance(rd, dict):
            raise ValueError(f"Gasstation returned non-dict from {url=!r}: {rd=!r}")
        return rd

    async def _request_gasstation_full_cached(self, url: str) -> dict:
        cache = GASSTATION_CACHE.get(url)
        if cache:
            ts, data = cache
            age = time.time() - ts
            if age < self.gasstation_cache_ttl_sec:
                self.logger.debug("Gasstation cache hit: url=%r, age=%r", url, age)
                return data
            self.logger.debug("Gasstation cache expired: url=%r, age=%r", url, age)
        else:
            self.logger.debug("Gasstation cache miss: url=%r", url)

        ts = time.time()
        data = await self._request_gasstation_full(url)
        GASSTATION_CACHE[url] = (ts, data)
        return data

    async def _request_gasstation(self, url: str, *, cached: bool = True) -> Any:
        if cached and self.gasstation_cache_ttl_sec:
            rd = await self._request_gasstation_full_cached(url)
        else:
            rd = await self._request_gasstation_full(url)
        data = rd.get(self.gasstation_key)
        if not data:
            raise ValueError(f"Gasstation returned no data for {self.gasstation_key=!r} from {url=!r}: {rd=!r}")
        return data

    async def _build_gas_price_polygon(self) -> TxParamsSimple:
        """
        For polygon, RPC nodes don't always return appropriate fee data,
        so a special API has to be requested.
        For context, see:
        https://docs.polygon.technology/tools/gas/polygon-gas-station/?h=zkevm#mainnet
        https://github.com/ethers-io/ethers.js/issues/2828#issuecomment-1283014250
        https://support.polygon.technology/support/solutions/articles/82000906165-how-to-resolve-the-transaction-underprice-issue-
        -> https://gist.github.com/grsLammy/a2eb6afeda700626da8d94664936852e#file-fetchgasprice-ts
        """
        url = POLYGON_GASSTATION_URL
        data = await self._request_gasstation(url)
        if not isinstance(data, dict):
            raise ValueError(f"Gasstation returned non-dict data for {self.gasstation_key=!r} from {url=!r}: {data=!r}")
        return TxParamsSimple(
            maxFeePerGas=hex(gwei_to_wei(data["maxFee"])),
            maxPriorityFeePerGas=hex(gwei_to_wei(data["maxPriorityFee"])),
        )

    async def _build_gas_price_polygonzkevm(self) -> TxParamsSimple:
        data = await self._request_gasstation(POLYGONZKEVM_GASSTATION_URL)
        return TxParamsSimple(
            gasPrice=hex(gwei_to_wei(data)),
        )

    async def build_gas_price_base(self) -> TxParamsSimple:
        # To consider: make gasstation chains exempt from the `_add_extra_gas_price`
        # (move them to `def build_gas_price`)
        if self.chain_id == 137:
            return await self._build_gas_price_polygon()

        if self.chain_id == 1101:
            return await self._build_gas_price_polygonzkevm()

        if self.chain_id in PRE_EIP1559_CHAIN_IDS:
            return await _build_gas_price_legacy(req_node=self.req_node)

        try:
            return await _build_gas_price_dynamic(req_node=self.req_node)
        except MethodUnavailableSimple:
            self.logger.error("Failed to build EIP-1559 gas on chain_id=%r", self.chain_id)
            return await _build_gas_price_legacy(req_node=self.req_node)

    def _add_extra_gas_price_and_units(self, tx_params: TxParamsSimple) -> TxParamsSimple:
        return _add_extra_gas_price_and_units(
            tx_params,
            gas_price_extra_pct=self.gas_price_extra_pct,
            gas_priority_fee_extra_pct=self.gas_priority_fee_extra_pct,
            gas_units_extra_pct=self.gas_units_extra_pct,
        )

    async def build_gas_params_pre(self, tx_params: TxParamsSimple) -> TxParamsSimple:
        # merlin, linea: `from` is required for estimate-gas.
        if self.chain_id in (4200, 59144) and not tx_params.get("from"):
            raise GasError({"message": "Tx params need specified `from` for linea and merlin"})

        if self.chain_id == 59144:  # linea
            return await _build_gas_params_linea(tx_params, req_node=self.req_node)

        # To consider: request these simultaneously
        # (which means not passing the gas price params to `eth_estimateGas`)
        tx_params_gas_price = await self.build_gas_price_base()
        gas_units_params = await _build_gas_units({**tx_params, **tx_params_gas_price}, req_node=self.req_node)
        return {**tx_params_gas_price, "gas": gas_units_params.get("gas", "0x0")}

    async def build_gas_params(self, tx_params: TxParamsSimple) -> TxParamsSimple:
        pre_result = await self.build_gas_params_pre(tx_params)
        # To consider: skip extra-price in the gasstation chains (polygon, polygonzkevm)
        return self._add_extra_gas_price_and_units(pre_result)
