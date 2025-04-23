import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, TypeVar, cast

import fastapi
import fastapi.responses
from fastapi import Body, Depends, HTTPException, Query
from hyapp.datetimes import dt_now
from hyapp.traces import TRACE_ID_VAR

from .blockchains import CHAIN_BY_ID, CHAIN_BY_NAME
from .common import SIMPLE_CHAIN_INFOS
from .evmrpc.evmrpc_check import evmrpc_check
from .evmrpc.evmrpc_client import EVMRPCErrorException, EVMRPCRequestParams
from .evmrpc.evmrpc_models import EVMRPCResponse
from .settings import Settings

if TYPE_CHECKING:
    from .evmrpc.evmrpc_client import EVMRPCClient

LOGGER = logging.getLogger(__name__)
API_ROUTER = fastapi.APIRouter()

# could be `Mapping[str, TJSONDumpable]`, but fastapi doesn't like that.
TResponse = dict[str, Any]


def settings_extract(request: fastapi.Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


TSettingsDep = Annotated[Settings, Depends(settings_extract)]


@API_ROUTER.get("/ping")
async def get_ping(request: fastapi.Request) -> dict:
    result = dict(message="pong", url=str(request.url), headers=dict(request.headers), now=dt_now().isoformat())
    LOGGER.debug("Ping result: %r", result)
    if "RAISE" in str(request.url):
        raise Exception("Test raise")
    return result


TWrapped = TypeVar("TWrapped", bound=Callable[..., Awaitable[Any]])


def wrap_common(title: str | None = None) -> Callable[[TWrapped], TWrapped]:
    def wrap_common_configured(func: TWrapped) -> TWrapped:
        actual_title = title or func.__name__

        @functools.wraps(func)
        async def wrapped_common(*args: Any, **kwargs: Any) -> Any:
            start_time = time.monotonic()

            try:
                result = await func(*args, **kwargs)
            except fastapi.exceptions.HTTPException:
                time_diff = round(time.monotonic() - start_time, 3)
                LOGGER.exception("Raised http error in %r", actual_title, extra=dict(x_timing=time_diff))
                raise
            except Exception:
                time_diff = round(time.monotonic() - start_time, 3)
                LOGGER.exception("Uncaught error in %r", actual_title, extra=dict(x_timing=time_diff))
                data = dict(status="Internal Error", trace_id=TRACE_ID_VAR.get())
                return fastapi.responses.JSONResponse(data, status_code=fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR)

            time_diff = round(time.monotonic() - start_time, 3)
            LOGGER.debug("Finished %r in %.3fs", actual_title, time_diff, extra=dict(x_timing=time_diff))
            return result

        return cast("TWrapped", wrapped_common)

    return wrap_common_configured


@API_ROUTER.post("/api/v1/evmrpc_check/")
@wrap_common()
async def evmrpc_check_v1(
    request: fastapi.Request,
    settings: TSettingsDep,
    *,
    token: Annotated[str, Query] = "",
    sequential: Annotated[bool, Query] = False,
    chain_names: Annotated[str, Query] = "",
) -> Any:
    evmrpc_cli: EVMRPCClient = request.app.state.evmrpccli

    auth_tokens_config = settings.opts.evmrpc_auth_tokens
    requester = auth_tokens_config.get(token)
    if requester is None:
        raise HTTPException(status_code=403, detail="Invalid authentication")

    return_all = bool(request.query_params.get("return_all"))
    results = await evmrpc_check(
        evmrpc_cli=evmrpc_cli,
        sequential=sequential,
        chain_by_name=SIMPLE_CHAIN_INFOS,
        chain_names=chain_names.split(",") if chain_names else None,
    )
    if not return_all:
        results = [item for item in results if not item.get("success")]
    return fastapi.responses.JSONResponse(dict(results=results))


COMPAT_CHAIN_NAME_MAP: dict[str, str] = {
    "b2": "bsquared",
}


@API_ROUTER.post("/api/v1/evmrpc/{chain}")  # noqa: FS003 f-string missing prefix
@wrap_common()
async def evmrpcproxy(
    request: fastapi.Request,
    settings: TSettingsDep,
    chain: int | str,
    data: Annotated[Any, Body()],
    log_extra: str = "",
    token: Annotated[str, Query] = "",
    *,
    mangle_getlogs: Annotated[bool, Query] = False,
) -> Any:
    evmrpc_cli: EVMRPCClient = request.app.state.evmrpccli

    auth_tokens_config = settings.opts.evmrpc_auth_tokens
    requester = auth_tokens_config.get(token)
    if requester is None:
        raise HTTPException(status_code=403, detail="Invalid authentication")

    chain_maybe_name = str(chain).lower()
    chain_maybe_name = COMPAT_CHAIN_NAME_MAP.get(chain_maybe_name, chain_maybe_name)

    chain_config = CHAIN_BY_NAME.get(chain_maybe_name)
    if not chain_config and (isinstance(chain, int) or chain.isdigit()):
        chain_config = CHAIN_BY_ID.get(int(chain))
    if not chain_config:
        raise HTTPException(status_code=404, detail=f"Chain not found: {chain!r}")

    context = {"requester": requester}
    if log_extra:
        context["x_extra"] = log_extra

    # A debug parameter for use in case of suspected difference between direct and proxied requests.
    node_name = request.query_params.get("x_node_name")

    try:
        evmrpc_resp = await evmrpc_cli.request(
            chain_name=chain_config["shortname"].lower(),
            data=data,
            node_name=node_name,
            context=context,
            req_params=EVMRPCRequestParams(allow_getlogs_mangle=mangle_getlogs, chain_id=chain_config["id"]),
        )
    except EVMRPCErrorException as exc:
        resp_data = (
            {**exc.last_response.data, "x_error_message": exc.message, "x_http_status": exc.last_status}
            if isinstance(exc.last_response, EVMRPCResponse) and isinstance(exc.last_response.data, dict)
            else exc.last_response.data
            if exc.last_response
            else {"error": "unknown error"}
        )
        return fastapi.responses.JSONResponse(
            resp_data,
            status_code=exc.last_status or fastapi.status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    resp_headers: dict[str, str] = {
        "X-EVMRPC-Node": evmrpc_resp.req.node_config.node_name,
        "X-EVMRPC-Attempt": str(evmrpc_resp.req.try_n),
    }
    return fastapi.responses.JSONResponse(evmrpc_resp.data, headers=resp_headers)
