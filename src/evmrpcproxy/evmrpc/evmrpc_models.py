import dataclasses
from typing import Any, NamedTuple, Self

from .evmrpc_config_model import EVMRPCNodeConfig

EVMRPC_NO_CODE: int = 0


class NoNodesAvailable(KeyError):
    """Raised when a chain with no viable nodes is requested"""


@dataclasses.dataclass(frozen=True)
class EVMRPCRequestParams:
    allow_getlogs_mangle: bool = False
    # A static `chain_id` to avoid unnecessary `eth_chainId` requests.
    # Might be required by some middleware classes.
    chain_id: int | None = None


@dataclasses.dataclass(frozen=True)
class EVMRPCRequestBase:
    data: dict | list
    node_config: EVMRPCNodeConfig
    req_params: EVMRPCRequestParams
    try_n: int
    # To avoid losing some field values on single<->batch transitions,
    # avoid having fields with default values here.

    def replace(self, **kwargs: Any) -> Self:
        return dataclasses.replace(self, **kwargs)


@dataclasses.dataclass(frozen=True)
class EVMRPCRequestSingle(EVMRPCRequestBase):
    data: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class EVMRPCRequestBatch(EVMRPCRequestBase):
    data: list[dict[str, Any]]


EVMRPCRequest = EVMRPCRequestSingle | EVMRPCRequestBatch


def req_to_singles(req: EVMRPCRequest) -> list[EVMRPCRequestSingle]:
    if isinstance(req, EVMRPCRequestSingle):
        assert isinstance(req.data, dict)
        return [req]

    assert isinstance(req.data, list)
    return [
        EVMRPCRequestSingle(data=subreq, node_config=req.node_config, req_params=req.req_params, try_n=req.try_n)
        for subreq in req.data
    ]


def req_from_singles(reqs: list[EVMRPCRequestSingle], *, req_to_match: EVMRPCRequest | None) -> EVMRPCRequest:
    if not reqs:
        raise ValueError("Cannot combine zero requests")

    if len(reqs) == 1:
        req = reqs[0]
        assert isinstance(req, EVMRPCRequestSingle)
        assert isinstance(req.data, dict)

        if req_to_match is None or isinstance(req_to_match, EVMRPCRequestSingle):
            return req

        # Otherwise, req_to_match is a batch
        assert isinstance(req_to_match, EVMRPCRequestBatch)
        assert isinstance(req_to_match.data, list)
        assert len(req_to_match.data) == 1
        return req.replace(data=[req.data])

    ref_req = reqs[0].replace(data=None)
    misplaced = [req for req in reqs if req.replace(data=None) != ref_req]
    if misplaced:
        raise ValueError("Mismatch in single-requests", misplaced)

    return EVMRPCRequestBatch(
        data=[req.data for req in reqs],
        node_config=ref_req.node_config,
        req_params=ref_req.req_params,
        try_n=ref_req.try_n,
    )


class EVMRPCResponse(NamedTuple):
    data: list | dict
    req: EVMRPCRequest

    @classmethod
    def from_single_req(cls, req: EVMRPCRequestSingle, result: Any) -> Self:
        assert isinstance(req.data, dict)
        resp_data = {
            "jsonrpc": req.data.get("jsonrpc") or "2.0",
            "id": req.data.get("id"),
            "result": result,
        }
        return cls(data=resp_data, req=req)

    @property
    def has_errors(self) -> bool:
        try:
            if isinstance(self.data, dict):
                return "error" in self.data
            return any("error" in item for item in self.data)
        except Exception:
            return False

    def replace(self, **kwargs: Any) -> Self:
        return self._replace(**kwargs)


class EVMRPCResponseError(NamedTuple):
    code: int
    message: str
    resp: EVMRPCResponse

    @classmethod
    def parse_one(cls, resp_data_item: Any, resp: EVMRPCResponse) -> Self | None:
        if not isinstance(resp_data_item, dict):
            return cls(code=EVMRPC_NO_CODE, message="Non-dict response", resp=resp)

        error = resp_data_item.get("error")
        if not error:
            return None

        if not isinstance(error, dict):
            return cls(code=EVMRPC_NO_CODE, message="Non-dict error", resp=resp)

        code = error.get("code", EVMRPC_NO_CODE)
        message = error.get("message", "")

        if not isinstance(message, str):
            message = repr(message)
        if not isinstance(code, int):
            code = EVMRPC_NO_CODE

        return cls(code=code, message=message, resp=resp)

    @classmethod
    def parse(cls, resp: EVMRPCResponse) -> list[Self]:
        if isinstance(resp.data, list):
            results = [cls.parse_one(item, resp=resp) for item in resp.data]
            return [item for item in results if item is not None]

        result = cls.parse_one(resp.data, resp=resp)
        return [result] if result is not None else []

    def dump_for_log(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "raw": self.resp.data}


@dataclasses.dataclass(kw_only=True)
class EVMRPCErrorException(Exception):
    exc: Exception | None
    last_response: EVMRPCResponse | None
    last_status: int
    message: str = "EVMRPC error"

    def replace(self, **kwargs: Any) -> Self:
        return dataclasses.replace(self, **kwargs)


@dataclasses.dataclass(kw_only=True)
class EVMRPCErrorResponseException(EVMRPCErrorException):
    last_response: EVMRPCResponse
    exc: Exception | None = None
    message: str = "EVMRPC response error"
    last_status: int = 0
