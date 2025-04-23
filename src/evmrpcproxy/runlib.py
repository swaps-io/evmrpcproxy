import hyapp.logs as hyapp_logs
import sentry_sdk

from .settings import Settings

INIT_STATE: dict[str, Settings] = {}


def init_logs(settings: Settings) -> None:
    if settings.opts.env in ("dev", "tests"):
        hyapp_logs.init_dev_logs()
        return

    hyapp_logs.init_logs()

    if settings.opts.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.opts.sentry_dsn,
            # Set traces_sample_rate to 1.0 to capture 100%
            # of transactions for performance monitoring.
            traces_sample_rate=1.0,
        )


def init_all(settings: Settings | None = None) -> None:
    if settings is None:
        settings = Settings()

    # Allow for re-calling.
    # Needed to ensure initialization when under e.g. gunicorn.
    if INIT_STATE:
        prev_settings = INIT_STATE["settings"]
        if settings != prev_settings:
            raise Exception(f"Trying to initialize with different settings: {settings=!r} != {prev_settings=!r}")
        return
    INIT_STATE["settings"] = settings

    init_logs(settings)
