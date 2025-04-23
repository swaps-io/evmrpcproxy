import contextlib
import functools
import logging
from collections.abc import AsyncGenerator

import fastapi
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from hyapp.api import TraceIdMiddleware

from .api_handlers import API_ROUTER
from .common import make_evmrpc_cli
from .runlib import init_all
from .settings import Settings

LOGGER = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def api_app_lifespan(settings: Settings, app: fastapi.FastAPI) -> AsyncGenerator[None, None]:
    init_all(settings)

    async with contextlib.AsyncExitStack() as lifespan_acm:
        app.state.acm = lifespan_acm

        app.state.evmrpccli = await lifespan_acm.enter_async_context(make_evmrpc_cli(settings).manage_ctx())

        yield

    app.state.acm = None


def build_app(settings: Settings | None = None) -> fastapi.FastAPI:
    if settings is None:
        settings = Settings()
    app = fastapi.FastAPI(lifespan=functools.partial(api_app_lifespan, settings))
    app.state.settings = settings
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
