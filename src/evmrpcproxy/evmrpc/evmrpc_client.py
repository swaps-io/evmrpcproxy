from __future__ import annotations

import contextlib
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Any, Self, Sequence

from hyapp.https import HTTPClient

from .evmrpc_middleware import (
    EVMRPCChainIdMiddleware,
    EVMRPCExtGasMiddleware,
    EVMRPCMangleGetlogsMiddleware,
    EVMRPCMiddlewareBase,
    EVMRPCUnbatchMiddleware,
    TEVMRPCHandler,
)
from .evmrpc_models import (
    EVMRPCErrorException,
    EVMRPCErrorResponseException,
    EVMRPCRequest,
    EVMRPCRequestBatch,
    EVMRPCRequestParams,
    EVMRPCRequestSingle,
    EVMRPCResponse,
    EVMRPCResponseError,
    NoNodesAvailable,
)
from .evmrpc_utils import is_evmrpc_error_response_retriable
from .utils import dumpcut

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .evmrpc_config_model import (
        EVMRPCConfig,
        EVMRPCNodeConfig,
        EVMRPCSecrets,
        TEVMRPCChains,
    )


LOGGER = logging.getLogger(__name__)

DEFAULT_MIDDLEWARES: Sequence[type[EVMRPCMiddlewareBase]] = [
    # **ordering**: the topmost one calls the next one in chain
    # (same as MRO and same as `@decorator`).
    # Pick out and handle `ext_estimateGas` requests.
    EVMRPCExtGasMiddleware,
    # Pick out `eth_chainId` and make a result without any network calls.
    EVMRPCChainIdMiddleware,
    # Alter `eth_getLogs` so that it returns at least something instead of failing.
    # (controlled by a query parameter, normally)
    EVMRPCMangleGetlogsMiddleware,
    # Make separate requests on bouncebit which doesn't support batches.
    # This one should (probably) be at the bottom.
    EVMRPCUnbatchMiddleware,
]


def _make_http_cli() -> HTTPClient:
    # Retries and logging are handled on this side.
    # To consider: might actually use `aiohttp.ClientSession()` directly
    # (but the error handling is still being used).
    logger = LOGGER.getChild("http")
    logger.setLevel("INFO")
    return HTTPClient(retry_attempts=1, max_resp_log_len=16384, logger=logger)


@dataclasses.dataclass()
class EVMRPCClient:
    evmrpc_config: EVMRPCConfig
    evmrpc_secrets: EVMRPCSecrets
    http_cli: HTTPClient = dataclasses.field(default_factory=_make_http_cli)

    do_upstream_debug: bool = False
    retry_attempts: int = 5
    # Request log size is somewhat smaller, due to the assumption
    # that requests are more reproducible from context than responses.
    # As an example, web3.py doesn't log requests at all.
    max_req_log_size: int = 10_000
    max_resp_log_size: int = 16_000
    middlewares: Sequence[type[EVMRPCMiddlewareBase]] = dataclasses.field(default_factory=lambda: DEFAULT_MIDDLEWARES)

    logger: logging.Logger = LOGGER

    @property
    def evmrpc_chains(self) -> TEVMRPCChains:
        return self.evmrpc_config.chains

    def __post_init__(self) -> None:
        self.rotation_order = {chain_name: list(nodes) for chain_name, nodes in self.evmrpc_chains.items()}

    @contextlib.asynccontextmanager
    async def manage_ctx(self) -> AsyncGenerator[Self, None]:
        async with self.http_cli:
            yield self

    def _make_req_log_context(self, data: Any) -> dict[str, Any]:
        return dumpcut(data=data, max_length=self.max_req_log_size, full_key="x_request", cut_key="x_request_cut")

    def _make_resp_log_context(self, data: Any) -> dict[str, Any]:
        return dumpcut(data=data, max_length=self.max_resp_log_size, full_key="x_response", cut_key="x_response_cut")

    def get_all_node_configs(self, chain_name: str) -> list[EVMRPCNodeConfig]:
        names = self.rotation_order.get(chain_name)
        if not names:
            raise NoNodesAvailable(chain_name)
        return [self.evmrpc_chains[chain_name][name] for name in names]

    def get_node_config(self, chain_name: str, *, rotate: bool = False) -> EVMRPCNodeConfig:
        names = self.rotation_order.get(chain_name)
        if not names:
            raise NoNodesAvailable(chain_name)

        if rotate:
            # mutate inplace
            names.append(names.pop(0))

        return self.evmrpc_chains[chain_name][names[0]]

    def _check_response(self, resp: EVMRPCResponse) -> None:
        errors = EVMRPCResponseError.parse(resp)
        if not errors:
            return

        retriable = any(is_evmrpc_error_response_retriable(item.code, item.message) for item in errors)
        self.logger.error(
            "EVMRPC response error",
            extra={
                "chain": resp.req.node_config.chain_name,
                "evmrpc_node": resp.req.node_config.node_name,
                "try_n": resp.req.try_n,
                "x_request": resp.req.data,
                "x_evmrpc_response_errors": [item.dump_for_log() for item in errors],
                "retriable": retriable,
            },
        )

        if retriable:
            raise EVMRPCErrorResponseException(last_response=resp)
        # If not retriable, the error-response should get returned as-is,
        # same as non-error responses.

    async def _request_one_call(self, req: EVMRPCRequest) -> EVMRPCResponse:
        url = req.node_config.get_url(self.evmrpc_secrets)
        http_resp = await self.http_cli.req(
            url=url, method="post", headers=req.node_config.headers, json=req.data, require_ok=False
        )
        http_resp_ok = http_resp.status == 200
        if self.do_upstream_debug:
            self.logger.debug(
                "EVMRPC upstream response",
                extra={
                    "x_url": url,
                    "x_req_data": req.data,
                    "x_resp_status": http_resp.status,
                    "x_resp_body": http_resp.content.decode("utf-8", errors="replace"),
                },
            )
        try:
            resp_data = http_resp.json()
            if not isinstance(resp_data, (dict, list)):
                raise ValueError("Expected a list or dict response")
        except Exception as exc:
            resp_data = http_resp.content.decode("utf-8", errors="replace")
            resp = EVMRPCResponse(data={"__raw__": resp_data}, req=req)
            raise EVMRPCErrorException(
                exc=None,
                last_response=resp,
                message=(
                    "EVMRPC response failed to parse as JSON list/dict"
                    if http_resp_ok
                    else "EVMRPC error failed to parse as JSON list/dict"
                ),
                last_status=http_resp.status,
            ) from exc

        resp = EVMRPCResponse(data=resp_data, req=req)

        # Tricky point: try handle RPC-level errors first, then HTTP-level errors,
        # because the HTTP status can vary,
        # and RPC-level-error handling is more special.
        self._check_response(resp)

        if not http_resp_ok:
            raise EVMRPCErrorException(
                exc=None, last_response=resp, last_status=http_resp.status, message="EVMRPC node error status"
            )

        return resp

    async def _request_one_node(self, req: EVMRPCRequest) -> EVMRPCResponse:
        straight_handler: TEVMRPCHandler = self._request_one_call
        all_nodes = self.get_all_node_configs(req.node_config.chain_name)

        next_handler: TEVMRPCHandler = straight_handler
        middleware_names: list[str] = []
        # Since this is wrapping rather than calling, start from the bottom one.
        for middleware_cls in self.middlewares[::-1]:
            middleware = middleware_cls(
                next_handler=next_handler, straight_handler=straight_handler, all_nodes=all_nodes, logger=self.logger
            )
            middleware_names.append(middleware.name)
            next_handler = middleware.handle

        self.logger.debug("Middleware onion", extra={"x_middlewares": middleware_names[::-1]})
        return await next_handler(req)

    async def request(
        self,
        chain_name: str,
        data: dict | list,
        node_name: str | None = None,
        *,
        context: dict | None = None,
        req_params: EVMRPCRequestParams = EVMRPCRequestParams(),
    ) -> EVMRPCResponse:
        common_log_context = {"chain": chain_name, **(context or {})}
        req_log_context = self._make_req_log_context(data)

        retry_attempts = 1 if node_name else self.retry_attempts
        node_config = self.evmrpc_chains[chain_name][node_name] if node_name else self.get_node_config(chain_name)

        start_time = time.monotonic()
        for try_n in range(retry_attempts):
            current_try_log_context = {**common_log_context, "evmrpc_node": node_config.node_name, "try_n": try_n}
            node_start_time = time.monotonic()

            req: EVMRPCRequest
            if isinstance(data, list):
                req = EVMRPCRequestBatch(node_config=node_config, data=data, req_params=req_params, try_n=try_n)
            else:
                req = EVMRPCRequestSingle(node_config=node_config, data=data, req_params=req_params, try_n=try_n)

            try:
                resp = await self._request_one_node(req)
            except Exception as exc:
                final = try_n + 1 >= retry_attempts
                end_time = time.monotonic()
                log_extra = {
                    **current_try_log_context,
                    "x_evmrpc_error": repr(exc),
                    "x_node_time": round(end_time - node_start_time, 3),
                    "x_total_time": round(end_time - start_time, 3),
                }

                if final:
                    self.logger.error("EVMRPC final error", extra={**req_log_context, **log_extra})

                    # Return the RPC-level error responses directly.
                    if isinstance(exc, EVMRPCErrorResponseException):
                        return exc.last_response

                    raise

                self.logger.error("EVMRPC error", extra=log_extra)
                node_config = self.get_node_config(chain_name, rotate=True)
                continue

            # For non-retriable error responses,
            # force node rotation anyway, just in case.
            if resp.has_errors:
                self.get_node_config(chain_name, rotate=True)

            end_time = time.monotonic()
            self.logger.info(
                "EVMRPC result",
                extra={
                    **req_log_context,
                    **self._make_resp_log_context(resp.data),
                    **current_try_log_context,
                    "x_node_time": round(end_time - node_start_time, 3),
                    "x_total_time": round(end_time - start_time, 3),
                },
            )
            return resp

        raise Exception("Logic error: this line should never be reached")
