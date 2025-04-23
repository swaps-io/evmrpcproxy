import asyncio

import typer

from .runlib import init_all
from .settings import Settings


def api_main_cli() -> None:
    """
    Serve the HTTP API.
    Configuration is through the environment variables.
    """
    from .api_app import main_run

    settings = Settings()
    init_all(settings)
    main_run(settings)


def tasks_main_cli(*, once: bool = False) -> None:
    from .tasks import Tasks

    settings = Settings()
    init_all(settings)
    worker = Tasks()
    asyncio.run(worker.run(once=once))


CLI_APP = typer.Typer()
CLI_APP.command("api")(api_main_cli)
CLI_APP.command("tasks")(tasks_main_cli)


def main() -> None:
    CLI_APP()


if __name__ == "__main__":
    main()
