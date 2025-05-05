import asyncio
import dataclasses
import datetime
import logging
import time
from collections.abc import Sequence
from typing import Any, Generic, NamedTuple, Self, TypeVar

import orjson
from hyapp.https import HTTPClient

LOGGER = logging.getLogger(__name__)


class RequestContext(NamedTuple):
    env: str
    chain: str
    requester: str
    x_requester: str
    method: str

    def dict(self) -> dict[str, Any]:
        return self._asdict()


class RequestStatsKey(NamedTuple):
    """Counters key for the requested upstream nodes"""

    env: str

    # final => returned to the user, otherwise retried.
    final: bool

    chain: str
    requester: str
    success: bool

    x_requester: str
    method: str

    node: str
    try_n: int

    def replace(self, **kwargs: Any) -> Self:
        return self._replace(**kwargs)


REQUEST_STATS_COLUMNS: tuple[str, ...] = (*RequestStatsKey._fields, "ts", "count")
TStatsKey = TypeVar("TStatsKey", bound=RequestStatsKey)


@dataclasses.dataclass()
class CHClient:
    ch_table_name: str
    ch_table_column_names: Sequence[str]
    ch_url: str = dataclasses.field(repr=False)
    http_cli: HTTPClient = dataclasses.field(repr=False)

    @staticmethod
    def _ch_qi(name: str) -> str:
        """Simple "quote identifier" for trusted input only"""
        if '"' in name:
            raise ValueError(f"Suspicious identifier: {name=!r}")
        return f'"{name}"'

    @staticmethod
    def serialize_ndjson(data: list[Any]) -> bytes:
        return b"".join(orjson.dumps(row, option=orjson.OPT_APPEND_NEWLINE) for row in data)

    def __post_init__(self) -> None:
        cols_sql = ", ".join(self._ch_qi(col) for col in self.ch_table_column_names)
        self.insert_query = f"insert into {self._ch_qi(self.ch_table_name)} ({cols_sql}) format JSONCompactEachRow"

    async def upload(self, data_rows: list[tuple[Any, ...]]) -> None:
        params = {"query": self.insert_query}
        headers: dict[str, str] = {}
        body = self.serialize_ndjson(data_rows)
        await self.http_cli.req(self.ch_url, method="post", params=params, headers=headers, data=body)


@dataclasses.dataclass()
class StatsUpdater(Generic[TStatsKey]):
    ch_cli: CHClient
    min_sync_period_sec: float = 60.0
    logger: logging.Logger = LOGGER

    def __post_init__(self) -> None:
        self.stats: dict[TStatsKey, int] = {}
        self.last_sync_mts = time.monotonic()
        self.upload_tasks: set[asyncio.Task] = set()

    def increment_stats_straight(self, key: TStatsKey, count: int = 1) -> None:
        self.stats[key] = self.stats.setdefault(key, 0) + count

    @property
    def is_upload_pending(self) -> bool:
        return time.monotonic() > self.last_sync_mts + self.min_sync_period_sec

    async def upload_stats_straight(self, data: dict[TStatsKey, int]) -> None:
        ts = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat()
        data_rows = [(*key, ts, count) for key, count in data.items()]
        await self.ch_cli.upload(data_rows)

    async def upload_stats(self) -> None:
        upload_data = self.stats
        self.stats = {}
        self.last_sync_mts = time.monotonic()
        try:
            await self.upload_stats_straight(upload_data)
        except Exception:
            LOGGER.exception("Error uploading stats")
            # Put the stats back in
            for key, count in upload_data.items():
                self.increment_stats_straight(key, count)

    async def increment_stats(self, key: TStatsKey, count: int = 1) -> None:
        self.increment_stats_straight(key, count)
        if self.is_upload_pending:
            # Upload in background
            task = asyncio.create_task(self.upload_stats())
            self.upload_tasks.add(task)
            task.add_done_callback(self.upload_tasks.discard)
            self.logger.debug(
                "Running stats upload task, total tasks: %d, stats size: %r", len(self.upload_tasks), len(self.stats)
            )
