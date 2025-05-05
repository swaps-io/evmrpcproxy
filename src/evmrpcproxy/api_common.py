from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from contextlib import AsyncExitStack

    from .evmrpc.evmrpc_client import EVMRPCClient
    from .settings import Settings
    from .stats import RequestStatsKey, StatsUpdater


@dataclasses.dataclass(frozen=True, kw_only=True)
class AppState:
    settings: Settings
    acm: AsyncExitStack
    evmrpccli: EVMRPCClient
    erp_request_stats: StatsUpdater[RequestStatsKey] | None = None

    def replace(self, **kwargs: Any) -> Self:
        return dataclasses.replace(self, **kwargs)
