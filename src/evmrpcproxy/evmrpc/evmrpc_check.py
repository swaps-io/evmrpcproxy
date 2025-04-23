from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NamedTuple

from hyapp.aio import aiogather_it
from hyapp.funcutils import groupby
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from collections.abc import Collection

    from .evmrpc_client import EVMRPCClient


class SimpleChainInfo(NamedTuple):
    id: int
    shortname: str
    multicall3_address: str | None
    non_evm: bool


class EVMRPCCheckResult(TypedDict):
    chain: str
    node: str
    res: Any
    exc: str | None
    block_number: int | None
    block_number_lag: int | None
    success: bool


# `Multicall3.aggregate3([])`
CHECK_REQ_MC_CALLDATA = "0x82ad56cb00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000000"
CHECK_RES_MC_DATA = "0x00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000000"


async def evmrpc_check(
    *,
    evmrpc_cli: EVMRPCClient,
    chain_by_name: dict[str, SimpleChainInfo],
    chain_names: Collection[str] | None = None,
    sequential: bool = False,
    max_block_number_lag: int | None = 10,
    per_chain_pause_sec: float = 0.0,
) -> list[EVMRPCCheckResult]:
    evmrpc_config = evmrpc_cli.evmrpc_config
    node_cfgs = [
        (chain_name, node_name)
        for chain_name, providers in evmrpc_config.chains.items()
        if chain_name in chain_by_name
        and not chain_by_name[chain_name].non_evm
        and (chain_names is None or chain_name in chain_names)
        for node_name in providers
    ]

    req_data = [
        {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
        {"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []},
    ]

    async def proc_one(chain_name: str, node_name: str) -> EVMRPCCheckResult:
        chain_cfg = chain_by_name[chain_name]

        chain_req_data = req_data
        if chain_cfg.multicall3_address:
            req_mc = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "eth_call",
                "params": [{"to": chain_cfg.multicall3_address, "data": CHECK_REQ_MC_CALLDATA}, "latest"],
            }
            chain_req_data = [*chain_req_data, req_mc]

        result = EVMRPCCheckResult(
            chain=chain_name,
            node=node_name,
            res=None,
            exc=None,
            block_number=None,
            block_number_lag=None,
            success=False,
        )
        try:
            resp = await evmrpc_cli.request(
                chain_name=chain_name,
                data=chain_req_data,
                node_name=node_name,
                context={"requester": "__evmrpc_check__"},
            )
            result["res"] = resp.data
            assert isinstance(resp.data, list)
            res_by_id = {item.get("id"): item for item in resp.data}

            chain_id = int(res_by_id[1]["result"], 16)
            if chain_id != chain_cfg.id:
                raise ValueError(f"Response {chain_id=!r} != {chain_cfg.id=!r}")

            block_number = int(res_by_id[2]["result"], 16)
            result["block_number"] = block_number

            mc_res = res_by_id.get(3)
            if mc_res is not None and mc_res.get("result") != CHECK_RES_MC_DATA:
                raise ValueError("Unexpected eth_call result")

            result["success"] = True
        except Exception as exc:
            result["success"] = False
            result["exc"] = repr(exc)

        return result

    results: list[EVMRPCCheckResult]
    if sequential:
        results = []
        prev_chain_name = None
        for chain_name, node_name in node_cfgs:
            if per_chain_pause_sec and prev_chain_name is not None and prev_chain_name != chain_name:
                await asyncio.sleep(per_chain_pause_sec)
            result = await proc_one(chain_name, node_name)
            results.append(result)
            prev_chain_name = chain_name
    else:
        results = await aiogather_it(proc_one(chain_name, node_name) for chain_name, node_name in node_cfgs)

    if max_block_number_lag is not None:
        max_bn_by_chain = {
            chain: max([res["block_number"] or 0 for res in chain_res if res.get("block_number")], default=0)
            for chain, chain_res in groupby((res["chain"], res) for res in results).items()
        }
        for res in results:
            bn = res.get("block_number")
            max_bn = max_bn_by_chain.get(res["chain"])
            if not bn or not max_bn:
                continue
            bn_lag = max_bn - bn
            res["block_number_lag"] = bn_lag
            if res.get("success") and bn_lag > max_block_number_lag:
                res["success"] = False
                res["exc"] = res.get("exc") or repr(ValueError(f"{bn_lag=!r} > {max_block_number_lag=!r}"))

    return results
