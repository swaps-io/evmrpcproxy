from collections.abc import Collection, Sequence

EVMRPC_NONRETRIABLE_CODES_RAW: Collection[int] = (
    # "execution reverted",
    3,
    # "Reverted", "VM execution error."
    -32015,
    # "OldNonce", "AlreadyKnown"
    -32010,
    # "the method ... does not exist/is not available"
    32601,
    # # "method not found"
    # # Unfortunately, have to make it retriable because some methods are only
    # # implemented by some of the nodes (e.g. `linea_estimateGas` not available on `infura`).
    # # No known use-cases for the `32601` above.
    # -32601,
)
EVMRPC_NONRETRIABLE_CODES = frozenset(EVMRPC_NONRETRIABLE_CODES_RAW)
EVMRPC_NONRETRIABLE_MESSAGES_RAW: Collection[str] = (
    # code: -32000, seen on `bouncebit`
    ": tx already in mempool",
    # code: -32000, seen on `polygonzkevm`
    "RPC error response: RPC error response: INTERNAL_ERROR: nonce too low",
)
EVMRPC_NONRETRIABLE_MESSAGES = frozenset(EVMRPC_NONRETRIABLE_MESSAGES_RAW)
EVMRPC_NONRETRIABLE_MESSAGE_PREFIXES: Sequence[str] = (
    # code: -32000, seen on `b2` `bsquared_public`
    "nonce too low: ",
    # code: -32000, seen on `bouncebit`
    # example: rpc error: code = Unknown desc = execution reverted: 0x5a421bd900000
    "rpc error: code = Unknown desc = execution reverted",
)


def is_evmrpc_error_response_retriable(code: int, message: str) -> bool:
    if code in EVMRPC_NONRETRIABLE_CODES:
        return False
    if message in EVMRPC_NONRETRIABLE_MESSAGES:
        return False
    if any(message.startswith(prefix) for prefix in EVMRPC_NONRETRIABLE_MESSAGE_PREFIXES):  # noqa: SIM103
        return False

    return True
