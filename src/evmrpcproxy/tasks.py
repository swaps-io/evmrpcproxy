import asyncio
import dataclasses
import logging
import time

from .common import SIMPLE_CHAIN_INFOS, make_evmrpc_cli
from .evmrpc.evmrpc_check import evmrpc_check
from .settings import Settings

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass()
class Tasks:
    run_pause_sec: float = 60.0
    settings: Settings = dataclasses.field(default_factory=Settings)
    logger: logging.Logger = LOGGER

    async def run_once(self) -> None:
        start_time = time.monotonic()
        evmrpc_cli = make_evmrpc_cli(self.settings)
        async with evmrpc_cli.manage_ctx():
            results = await evmrpc_check(
                evmrpc_cli=evmrpc_cli,
                sequential=True,
                chain_by_name=SIMPLE_CHAIN_INFOS,
                chain_names=None,
                per_chain_pause_sec=0.5,
            )
        time_taken_sec = time.monotonic() - start_time
        successes = [item for item in results if item["success"]]
        failures = [item for item in results if not item["success"]]
        chains = {item["chain"] for item in results}
        any_success_chains = {item["chain"] for item in successes}
        full_failure_chains = chains - any_success_chains
        any_failure_chains = {item["chain"] for item in failures}
        any_failure_nodes = {item["node"] for item in failures}
        extra_details = dict(
            x_successes=len(successes),
            x_failures=len(failures),
            x_chains=len(chains),
            x_failing_chains=sorted(any_failure_chains) or None,
            x_failing_nodes=sorted(any_failure_nodes) or None,
            x_full_failure_chains=sorted(full_failure_chains) or None,
            x_time_taken=time_taken_sec,
        )
        if failures:
            self.logger.error(
                "EVMRPC check returned %r/%r failures on %r/%r chains",
                len(failures),
                len(results),
                len(any_failure_chains),
                len(chains),
                extra=extra_details,
            )
        if full_failure_chains:
            self.logger.error(
                "EVMRPC check has fully failing chains: %s", ", ".join(sorted(full_failure_chains)), extra=extra_details
            )
        self.logger.info(
            "EVMRPC check results",
            extra=extra_details,
        )

    async def run(self, *, once: bool = False) -> None:
        while True:
            await self.run_once()
            if once:
                return

            sleep_time_sec = self.run_pause_sec
            self.logger.debug("Sleeping for %.3fs", sleep_time_sec)
            await asyncio.sleep(sleep_time_sec)
