from __future__ import annotations

import contextlib
import functools
import logging
from typing import TYPE_CHECKING

import fastapi
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from hyapp.api import TraceIdMiddleware
from hyapp.https import HTTPClient

from .api_common import AppState
from .api_handlers import API_ROUTER
from .common import make_evmrpc_cli
from .runlib import init_all
from .settings import Settings
from .stats import (
    REQUEST_STATS_COLUMNS,
    CHClient,
    RequestStatsKey,
    StatsUpdater,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


LOGGER = logging.getLogger(__name__)


async def _setup_ch_stats(app_state: AppState, settings: Settings, acm: contextlib.AsyncExitStack) -> AppState:
    ch_url = settings.opts.ch_url
    if not ch_url:
        return app_state

    http_cli = HTTPClient()
    await acm.enter_async_context(http_cli)

    ch_cli_reqs = CHClient(
        ch_table_name=settings.opts.ch_request_stats_table_name,
        ch_table_column_names=REQUEST_STATS_COLUMNS,
        ch_url=ch_url,
        http_cli=http_cli,
    )
    stats_reqs = StatsUpdater[RequestStatsKey](ch_cli=ch_cli_reqs)

    return app_state.replace(erp_request_stats=stats_reqs)


@contextlib.asynccontextmanager
async def api_app_lifespan(settings: Settings, app: fastapi.FastAPI) -> AsyncGenerator[None, None]:
    init_all(settings)

    async with contextlib.AsyncExitStack() as lifespan_acm:
        evmrpccli = await lifespan_acm.enter_async_context(make_evmrpc_cli(settings).manage_ctx())
        app_state = AppState(settings=settings, acm=lifespan_acm, evmrpccli=evmrpccli)
        app_state = await _setup_ch_stats(app_state=app_state, settings=settings, acm=lifespan_acm)

        app.state.app_state = app_state
        yield

    app.state.app_state = None


def build_app(settings: Settings | None = None) -> fastapi.FastAPI:
    if settings is None:
        settings = Settings()
    app = fastapi.FastAPI(lifespan=functools.partial(api_app_lifespan, settings))

    app.add_middleware(TraceIdMiddleware, trace_id_prefix="70")  # `p`rices
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(API_ROUTER)

    return app


API_APP = build_app()


def main_run(settings: Settings | None = None) -> None:
    app: fastapi.FastAPI | str
    if settings is None or settings.opts.env == "dev":
        settings = Settings()
        # Needed as string for `reload` (but might still not work):
        app = f"{__name__}:API_APP"
    else:
        # `reload` will not work.
        app = build_app(settings)

    if settings.opts.env == "dev":
        config = uvicorn.Config(
            app,
            host=settings.opts.api_bind,
            port=settings.opts.api_port,
            reload=settings.opts.api_dev_reload,
        )
    else:
        config = uvicorn.Config(
            app,
            host=settings.opts.api_bind,
            port=settings.opts.api_port,
            workers=settings.opts.api_deploy_workers,
            log_config=None,
        )

    server = uvicorn.Server(config)
    server.run()
