import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple, cast

import aiohttp
import pytest
import yaml
from hyapp.https import HTTPClient, HTTPResponse
from hyapp.jsons import json_dumps

from evmrpcproxy.common import make_evmrpc_cli
from evmrpcproxy.evmrpc.evmrpc_client import EVMRPCClient, EVMRPCErrorException
from evmrpcproxy.evmrpc.evmrpc_config_model import (
    EVMRPCConfig,
    EVMRPCNodeConfig,
    EVMRPCSecrets,
)
from evmrpcproxy.evmrpc.evmrpc_models import EVMRPCRequestParams, EVMRPCResponse
from evmrpcproxy.settings import Settings, SettingsOptsBase

SAMPLE_EVMRPC_CONFIG_YAML = """
mainnet:
  x_chain_id: 1
  quiknode: "https://bold-newest-putty.quiknode.pro/abc"
  infura: "https://mainnet.infura.io/v3/qwe"
bouncebit:
  x_chain_id: 6001
  blockvision: {url: "https://bouncebit-mainnet.blockvision.org/v1/...", max_blocks_distance: 1500, supports_batch: false}
  bouncebitapi_public: {url: "https://fullnode-mainnet.bouncebitapi.com/", max_blocks_distance: 1500, supports_batch: false}
"""
SAMPLE_EVMRPC_CONFIG = yaml.safe_load(SAMPLE_EVMRPC_CONFIG_YAML)


def test_evmrpc_config_settings() -> None:
    settings = Settings(
        opts=SettingsOptsBase(
            env="tests",
            evmrpc_config=SAMPLE_EVMRPC_CONFIG,
            evmrpc_secrets=EVMRPCSecrets(),
        ),
    )

    config = settings.opts.evmrpc_config
    assert isinstance(config, EVMRPCConfig)

    node_config = config.chains["bouncebit"]["blockvision"]
    assert isinstance(node_config, EVMRPCNodeConfig)
    assert node_config.max_blocks_distance == 1500
    assert not node_config.supports_batch

    assert config.chains["mainnet"]["infura"].supports_batch


class MockAHResponse(NamedTuple):
    status: int


@dataclasses.dataclass
class MockHTTPClient:
    handler: Callable
    requests: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    async def req(self, **kwargs: Any) -> HTTPResponse:
        self.requests.append(kwargs)
        try:
            result = self.handler(**kwargs)
            status = 200
        except aiohttp.ClientError as exc:
            result = {"detail": str(exc)}
            status = 500

        if isinstance(result, HTTPResponse):
            return result

        return HTTPResponse(
            orig=cast("aiohttp.ClientResponse", MockAHResponse(status=status)),
            content=json_dumps(result),
            time_taken_sec=0.0,
        )

    @property
    def req_bodies(self) -> list[Any]:
        return [req.get("json") for req in self.requests]

    def clear(self) -> None:
        self.requests = []


@pytest.fixture
def sample_settings() -> Settings:
    return Settings(
        opts=SettingsOptsBase(
            env="tests",
            evmrpc_config=SAMPLE_EVMRPC_CONFIG,
            evmrpc_secrets=EVMRPCSecrets(),
        ),
    )


def _handler_ok(**_: Any):
    return {"mock": 1}


def _handler_quikerr(url: str, **_: Any):
    if "quik" in url:
        raise aiohttp.ClientError("test raise")
    return {"mock": 2}


def _handler_err(**_: Any):
    raise aiohttp.ClientError("test raise 2")


async def test_evmrpc_rotation(sample_settings) -> None:
    http_mock = MockHTTPClient(handler=_handler_ok)
    evmrpc_client = make_evmrpc_cli(settings=sample_settings, http_cli=cast("HTTPClient", http_mock))
    chain_name = "mainnet"
    req_data = {"test_req": 1}

    # Straight success on first try
    http_mock.clear()
    resp = await evmrpc_client.request(chain_name, req_data)
    assert resp.req.node_config.node_name == "quiknode"
    assert resp.req.try_n == 0
    assert len(http_mock.requests) == 1

    # Failure on first try, success on second
    http_mock.clear()
    http_mock.handler = _handler_quikerr
    resp = await evmrpc_client.request(chain_name, req_data)
    assert resp.req.node_config.node_name == "infura"
    assert resp.req.try_n == 1
    assert len(http_mock.requests) == 2

    # Pinned node should remain
    http_mock.clear()
    http_mock.handler = _handler_ok
    resp = await evmrpc_client.request(chain_name, req_data)
    assert resp.req.node_config.node_name == "infura"
    assert resp.req.try_n == 0
    assert len(http_mock.requests) == 1

    # Complete failure
    http_mock.clear()
    http_mock.handler = _handler_err
    with pytest.raises(EVMRPCErrorException) as exc:
        resp = await evmrpc_client.request(chain_name, req_data)
    assert "test raise 2" in repr(exc)


def _pong_req_to_resp(item: dict[str, Any]) -> dict:
    assert isinstance(item, dict), item
    return {
        "jsonrpc": item.get("jsonrpc") or "2.0",
        "id": item.get("id"),
        "result": {"method": item.get("method"), "params": item.get("params")},
    }


@pytest.fixture
def pong_evmrpc_cli_and_mock(sample_settings) -> tuple[EVMRPCClient, MockHTTPClient]:
    def handler(**req_args: Any):
        req_data = req_args.get("json")
        if isinstance(req_data, list):
            return [_pong_req_to_resp(item) for item in req_data]
        assert isinstance(req_data, dict), req_data
        return _pong_req_to_resp(req_data)

    http_mock = MockHTTPClient(handler=handler)
    evmrpc_client = make_evmrpc_cli(settings=sample_settings, http_cli=cast("HTTPClient", http_mock))
    return evmrpc_client, http_mock


@pytest.fixture
def pong_evmrpc_cli(pong_evmrpc_cli_and_mock) -> EVMRPCClient:
    evmrpc_cli, _ = pong_evmrpc_cli_and_mock
    return evmrpc_cli


@pytest.fixture
def pong_http_mock(pong_evmrpc_cli_and_mock) -> MockHTTPClient:
    _, http_mock = pong_evmrpc_cli_and_mock
    return http_mock


TEVMRPCSimpleRequester = Callable[[list | dict], Awaitable[EVMRPCResponse]]


@pytest.fixture
def req_evmrpc_simple(pong_evmrpc_cli) -> TEVMRPCSimpleRequester:
    req_params = EVMRPCRequestParams(allow_getlogs_mangle=True, chain_id=1)

    async def req_evmrpc_simple_impl(data: list | dict) -> EVMRPCResponse:
        return await pong_evmrpc_cli.request("mainnet", data, req_params=req_params)

    return req_evmrpc_simple_impl


REQ_BLOCK_NUMBER = {"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []}
REQ_BLOCK_NUMBER_2 = {"jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber"}
REQ_CHAIN_ID = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId"}


async def test_evmrpc_middleware_chainid(
    req_evmrpc_simple: TEVMRPCSimpleRequester, pong_http_mock: MockHTTPClient
) -> None:
    resp = await req_evmrpc_simple([REQ_BLOCK_NUMBER, REQ_CHAIN_ID, REQ_BLOCK_NUMBER_2])
    assert resp.data == [
        {"jsonrpc": "2.0", "id": 2, "result": {"method": "eth_blockNumber", "params": []}},
        {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
        {"jsonrpc": "2.0", "id": 3, "result": {"method": "eth_blockNumber", "params": None}},
    ]
    # One batch request without the eth_chainId
    assert pong_http_mock.req_bodies == [[REQ_BLOCK_NUMBER, REQ_BLOCK_NUMBER_2]]


async def test_evmrpc_middleware_unbatch_chainid(pong_evmrpc_cli: EVMRPCClient, pong_http_mock: MockHTTPClient) -> None:
    req_params = EVMRPCRequestParams(allow_getlogs_mangle=True, chain_id=6001)
    resp = await pong_evmrpc_cli.request(
        "bouncebit", [REQ_BLOCK_NUMBER, REQ_CHAIN_ID, REQ_BLOCK_NUMBER_2], req_params=req_params
    )
    assert resp.data == [
        {"jsonrpc": "2.0", "id": 2, "result": {"method": "eth_blockNumber", "params": []}},
        {"jsonrpc": "2.0", "id": 1, "result": "0x1771"},
        {"jsonrpc": "2.0", "id": 3, "result": {"method": "eth_blockNumber", "params": None}},
    ]
    # Two independent requests (because it's bouncebit) without the eth_chainId
    assert pong_http_mock.req_bodies == [REQ_BLOCK_NUMBER, REQ_BLOCK_NUMBER_2]


async def test_evmrpc_middleware_single(
    req_evmrpc_simple: TEVMRPCSimpleRequester, pong_http_mock: MockHTTPClient
) -> None:
    resp = await req_evmrpc_simple(REQ_BLOCK_NUMBER)
    assert resp.data == {"jsonrpc": "2.0", "id": 2, "result": {"method": "eth_blockNumber", "params": []}}
    assert pong_http_mock.req_bodies == [REQ_BLOCK_NUMBER]


async def test_evmrpc_middleware_single_ext_error(
    req_evmrpc_simple: TEVMRPCSimpleRequester, pong_http_mock: MockHTTPClient
) -> None:
    """Non-batched requests should receive non-batched errors, even for `ext_estimateGas`"""

    def _handler(**_: Any):
        # `ext_estimateGas` internally does batched upstream requests.
        return [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Internal error"}}]

    pong_http_mock.handler = _handler
    req = {"jsonrpc": "2.0", "id": 1, "method": "ext_estimateGas", "params": [{}]}
    resp = await req_evmrpc_simple(req)
    assert resp.data == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Internal error"}}


async def test_evmrpc_middleware_batch(
    req_evmrpc_simple: TEVMRPCSimpleRequester, pong_http_mock: MockHTTPClient
) -> None:
    resp = await req_evmrpc_simple([REQ_BLOCK_NUMBER])
    assert resp.data == [{"jsonrpc": "2.0", "id": 2, "result": {"method": "eth_blockNumber", "params": []}}]
    assert pong_http_mock.req_bodies == [[REQ_BLOCK_NUMBER]]


async def test_evmrpc_middleware_single_chainid(
    req_evmrpc_simple: TEVMRPCSimpleRequester, pong_http_mock: MockHTTPClient
) -> None:
    resp = await req_evmrpc_simple(REQ_CHAIN_ID)
    assert resp.data == {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
    assert pong_http_mock.req_bodies == []  # No upstream requests


async def test_evmrpc_middleware_single_batch_chainid(
    req_evmrpc_simple: TEVMRPCSimpleRequester, pong_http_mock: MockHTTPClient
) -> None:
    # Batch request with one item => batch response with one item.
    resp = await req_evmrpc_simple([REQ_CHAIN_ID])
    assert resp.data == [{"jsonrpc": "2.0", "id": 1, "result": "0x1"}]
    assert pong_http_mock.req_bodies == []  # No upstream requests
