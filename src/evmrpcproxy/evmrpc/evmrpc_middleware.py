import abc
import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from hyapp.aio import aiogather, aiogather_it

from .evmrpc_config_model import EVMRPCNodeConfig
from .evmrpc_gas import (
    GasError,
    MethodUnavailableSimple,
    W3GasHelper,
    normalize_tx_params,
)
from .evmrpc_models import (
    EVMRPCErrorResponseException,
    EVMRPCRequest,
    EVMRPCRequestBatch,
    EVMRPCRequestSingle,
    EVMRPCResponse,
    req_from_singles,
    req_to_singles,
)
from .utils import pick_out_special_items, put_in_special_results

TEVMRPCHandler = Callable[[EVMRPCRequest], Awaitable[EVMRPCResponse]]


def _match_batch(resp: EVMRPCResponse, *, req: EVMRPCRequest) -> EVMRPCResponse:
    """Non-batched request should return a non-batched result"""
    if isinstance(req, EVMRPCRequestSingle):
        assert isinstance(req.data, dict)
        if isinstance(resp.data, dict):
            return resp
        assert isinstance(resp.data, list)
        assert len(resp.data) == 1
        return resp.replace(data=resp.data[0])
    return resp


@dataclasses.dataclass(frozen=True)
class EVMRPCMiddlewareBase(abc.ABC):
    # Next in the middleware onion
    next_handler: TEVMRPCHandler
    # Handler that skips all further middlewares.
    straight_handler: TEVMRPCHandler
    all_nodes: list[EVMRPCNodeConfig]
    logger: logging.Logger

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abc.abstractmethod
    async def handle(self, req: EVMRPCRequest) -> EVMRPCResponse:
        raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class EVMRPCMiddlewareNoop(EVMRPCMiddlewareBase):
    async def handle(self, req: EVMRPCRequest) -> EVMRPCResponse:
        return await self.next_handler(req)


@dataclasses.dataclass(frozen=True)
class EVMRPCSingleRequestPreprocessorMiddlewareBase(EVMRPCMiddlewareBase):
    @abc.abstractmethod
    async def process_single_req(self, req: EVMRPCRequestSingle) -> EVMRPCRequestSingle:
        raise NotImplementedError

    async def handle(self, req: EVMRPCRequest) -> EVMRPCResponse:
        reqs = req_to_singles(req)
        reqs_mangled = await aiogather_it(self.process_single_req(req) for req in reqs)
        req_mangled = req_from_singles(reqs_mangled, req_to_match=req)
        return await self.next_handler(req_mangled)


@dataclasses.dataclass(frozen=True)
class EVMRPCMangleGetlogsMiddleware(EVMRPCSingleRequestPreprocessorMiddlewareBase):
    def _mangle_eth_getlogs(self, req_data: dict, *, max_blocks_distance: int) -> dict:
        try:
            params = req_data["params"][0]
            from_block_hex = params["fromBlock"]
            to_block_hex = params["toBlock"]
        except (KeyError, IndexError):
            self.logger.exception("Error in EVMRPCMangleGetlogsMiddleware params")
            return req_data

        to_block: int | None = None
        try:
            from_block = int(from_block_hex, 16)
            to_block = int(to_block_hex, 16)
        except ValueError:
            self.logger.error(
                "Non-hex blocks in EVMRPCMangleGetlogsMiddleware params: from_block=%r, to_block=%r",
                from_block,
                to_block,
            )
            return req_data

        distance = to_block - from_block
        if distance > max_blocks_distance:
            new_from_block = to_block - max_blocks_distance
            new_from_block_hex = hex(new_from_block)
            self.logger.info(
                "Mangling eth_getLogs from %r blocks to %r (%r -> %r) .. %r",
                distance,
                max_blocks_distance,
                from_block_hex,
                new_from_block_hex,
                to_block_hex,
            )
            return {**req_data, "params": [{**params, "fromBlock": new_from_block_hex}]}
        return req_data

    async def process_single_req(self, req: EVMRPCRequestSingle) -> EVMRPCRequestSingle:
        if (
            req.req_params.allow_getlogs_mangle
            and req.node_config.max_blocks_distance
            and req.data.get("method") == "eth_getLogs"
        ):
            req_data = self._mangle_eth_getlogs(req.data, max_blocks_distance=req.node_config.max_blocks_distance)
            return req.replace(data=req_data)
        return req


@dataclasses.dataclass(frozen=True)
class EVMRPCUnbatchMiddleware(EVMRPCMiddlewareBase):
    async def handle(self, req: EVMRPCRequest) -> EVMRPCResponse:
        if not req.node_config.supports_batch and isinstance(req, EVMRPCRequestBatch):
            assert isinstance(req.data, list)
            reqs = req_to_singles(req)
            resps = await aiogather_it(self.next_handler(req) for req in reqs)
            return EVMRPCResponse(data=[resp.data for resp in resps], req=req)

        return await self.next_handler(req)


@dataclasses.dataclass(frozen=True)
class EVMRPCSingleRequestHandlerMiddlewareBase(EVMRPCMiddlewareBase):
    @abc.abstractmethod
    def is_req_relevant(self, req: EVMRPCRequestSingle) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def handle_single_req(self, req: EVMRPCRequestSingle) -> EVMRPCResponse:
        raise NotImplementedError

    async def _handle_normal(self, reqs: list[EVMRPCRequestSingle], top_req: EVMRPCRequest) -> EVMRPCResponse:
        if not reqs:
            return EVMRPCResponse(data=[], req=top_req)

        # For cases when there's exactly one "normal" request,
        # send it as non-batch, and wrap the response in a list,
        # to simplify further handling (and some logging cases).
        req = req_from_singles(reqs, req_to_match=None)
        resp = await self.next_handler(req)
        if isinstance(req, EVMRPCRequestSingle):
            assert len(reqs) == 1
            assert isinstance(resp.data, dict)
            return resp.replace(data=[resp.data])

        return resp

    async def _handle_relevant(self, reqs: list[EVMRPCRequestSingle], top_req: EVMRPCRequest) -> EVMRPCResponse:
        if not reqs:
            return EVMRPCResponse(data=[], req=top_req)

        resps = await aiogather_it(self.handle_single_req(req) for req in reqs)
        return EVMRPCResponse(data=[resp.data for resp in resps], req=top_req)

    async def handle(self, req: EVMRPCRequest) -> EVMRPCResponse:
        reqs = req_to_singles(req)
        reqs_normal, reqs_relevant_with_idx = pick_out_special_items(reqs, is_special=self.is_req_relevant)

        if not reqs_relevant_with_idx:
            # Straight pass-through.
            return await self.next_handler(req)

        reqs_relevant = [req for _, req in reqs_relevant_with_idx]
        if not reqs_normal:
            resp_relevant = await self._handle_relevant(reqs_relevant, top_req=req)
            # Non-batched request should return a non-batched result.
            return _match_batch(resp_relevant, req=req)

        resp_normal, resp_relevant = await aiogather(
            self._handle_normal(reqs_normal, top_req=req),
            self._handle_relevant(reqs_relevant, top_req=req),
        )

        if not isinstance(resp_normal.data, list):
            # Likely some error happened, ignore the special handlers.
            self.logger.warning(
                ("EVMRPCSingleRequestHandlerMiddlewareBase: ignoring relevant results due to non-list normal response")
            )
            return resp_normal

        data_normal = resp_normal.data
        data_relevant = resp_relevant.data
        assert isinstance(data_relevant, list)
        assert len(data_relevant) == len(reqs_relevant_with_idx)
        data_relevant_with_idx = [(idx, resp) for (idx, _), resp in zip(reqs_relevant_with_idx, data_relevant)]

        data_full = put_in_special_results(data_normal, data_relevant_with_idx)
        return resp_normal.replace(data=data_full)


class EVMRPCChainIdMiddleware(EVMRPCSingleRequestHandlerMiddlewareBase):
    def is_req_relevant(self, req: EVMRPCRequestSingle) -> bool:
        return req.data.get("method") == "eth_chainId" and req.req_params.chain_id is not None

    async def handle_single_req(self, req: EVMRPCRequestSingle) -> EVMRPCResponse:
        assert self.is_req_relevant(req)
        chain_id = req.req_params.chain_id
        assert chain_id is not None
        return EVMRPCResponse.from_single_req(req=req, result=hex(chain_id))


def _pick_unknown_method_errors(data: Any) -> list[Any] | None:
    if not isinstance(data, list):
        return None
    errors = [item.get("error") for item in data if isinstance(item, dict) and item.get("error")]
    return [item for item in errors if isinstance(item, dict) and item.get("code") in (32601, -32601)]


class EVMRPCExtGasMiddleware(EVMRPCSingleRequestHandlerMiddlewareBase):
    RPC_METHOD_NAME: ClassVar[str] = "ext_estimateGas"

    def is_req_relevant(self, req: EVMRPCRequestSingle) -> bool:
        return req.data.get("method") == self.RPC_METHOD_NAME

    async def _handle_fallback(self, req: EVMRPCRequestSingle) -> EVMRPCResponse:
        assert self.is_req_relevant(req)
        req_mangled = req.replace(data={**req.data, "method": "eth_estimateGas"})
        # This would lose on the batching, but this shouldn't normally happen anyway.
        return await self.next_handler(req_mangled)

    async def _req_node(self, reqs: list, *, top_req: EVMRPCRequest) -> list:
        reqs_processed = [{"jsonrpc": "2.0", "id": idx + 1, **req} for idx, req in enumerate(reqs)]

        req = EVMRPCRequestBatch(
            data=reqs_processed,
            node_config=top_req.node_config,
            req_params=top_req.req_params,
            try_n=top_req.try_n,
        )
        try:
            # To consider: alter the req to force turn-on the `do_upstream_debug`.
            resp = await self.next_handler(req)
        except EVMRPCErrorResponseException as exc:
            unknown_method_errors = _pick_unknown_method_errors(exc.last_response.data)
            if unknown_method_errors:
                raise MethodUnavailableSimple(unknown_method_errors) from exc
            raise

        if not isinstance(resp.data, list) or len(resp.data) != len(reqs):
            raise GasError({"message": "Upstream error", "x_reqs": reqs, "x_resp": resp.data})

        # To consider: reorder `resp.data` by `id` values

        unknown_method_errors = _pick_unknown_method_errors(resp.data)
        if unknown_method_errors:
            raise MethodUnavailableSimple(unknown_method_errors)

        errors = [item.get("error") for item in resp.data if isinstance(item, dict) and item.get("error")]
        if errors:
            raise GasError(errors[0])
        return [item.get("result") for item in resp.data]

    async def _handle_gas(self, chain_id: int, req_data: dict, *, top_req: EVMRPCRequest) -> dict:
        params = req_data.get("params")
        assert isinstance(params, list)
        if len(params) > 2:
            raise GasError({"message": "Expected at most 2 params"})
        if len(params) == 2 and params[-1] != "latest":
            raise GasError({"message": "Only `latest` block is supported"})
        data_work = {**params[0]}

        # To maybe be done later: error handling.
        gas_price_extra_pct = float(data_work.pop("x_gas_price_extra_pct", 20.0))
        gas_priority_fee_extra_pct = float(data_work.pop("x_gas_priority_fee_extra_pct", 10.0))
        gas_units_extra_pct = float(data_work.pop("x_gas_units_extra_pct", 100.0))
        gasstation_key = data_work.pop("x_gasstation_key", "fast")

        helper = W3GasHelper(
            chain_id=chain_id,
            req_node=functools.partial(self._req_node, top_req=top_req),
            gasstation_key=gasstation_key,
            gas_price_extra_pct=gas_price_extra_pct,
            gas_priority_fee_extra_pct=gas_priority_fee_extra_pct,
            gas_units_extra_pct=gas_units_extra_pct,
        )
        tx_params = normalize_tx_params(data_work)
        result = await helper.build_gas_params(tx_params)
        return dict(result)

    def _unwrap_single_exc(self, exc: EVMRPCErrorResponseException) -> EVMRPCErrorResponseException:
        if isinstance(exc.last_response.data, list):
            if len(exc.last_response.data) != 1:
                self.logger.warning("Ignoring upstream errors: %r", exc.last_response.data[1:])
            return exc.replace(last_response=exc.last_response.replace(data=exc.last_response.data[0]))
        return exc

    async def handle_single_req(self, req: EVMRPCRequestSingle) -> EVMRPCResponse:
        assert self.is_req_relevant(req)
        chain_id = req.req_params.chain_id
        if chain_id is None:
            self.logger.error("No chain id specified for %r", req.node_config.chain_name)
            return await self._handle_fallback(req)

        try:
            assert isinstance(req.data, dict)
            result = await self._handle_gas(chain_id, req.data, top_req=req)
            return EVMRPCResponse.from_single_req(req=req, result=result)
        except GasError as exc:
            error_data = exc.args[0]
            self.logger.warning("GasError in EVMRPCExtGasMiddleware: %r", error_data)
            resp_data = {
                "jsonrpc": req.data.get("jsonrpc") or "2.0",
                "id": req.data.get("id"),
                "error": error_data,
            }
            return EVMRPCResponse(data=resp_data, req=req)
        except EVMRPCErrorResponseException as exc:
            self.logger.error("Response error in EVMRPCExtGasMiddleware: %r", exc)
            # Since this middleware usually does batched upstream requests,
            # which return lists of errors,
            # but this is a single request,
            # make sure the last-error in the response is a single dict.
            exc_unwrapped = self._unwrap_single_exc(exc)
            raise exc_unwrapped from exc
        except Exception:
            self.logger.exception("Error in EVMRPCExtGasMiddleware")
            return await self._handle_fallback(req)
