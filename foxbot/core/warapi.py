from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = "FoxholeLogisticsBot/2.0 (Discord bot; polls every few minutes with ETags)"
REQUEST_TIMEOUT = 20.0
CONNECT_TIMEOUT = 10.0
MIN_REQUEST_INTERVAL = 0.5

WAR_PATH = "/worldconquest/war"
MAPS_PATH = "/worldconquest/maps"


def report_path(map_name: str) -> str:
    return f"/worldconquest/warReport/{map_name}"


def static_path(map_name: str) -> str:
    return f"/worldconquest/maps/{map_name}/static"


def dynamic_path(map_name: str) -> str:
    return f"/worldconquest/maps/{map_name}/dynamic/public"


@dataclass
class ApiResponse:
    status: int
    data: Any = None
    etag: str | None = None


@dataclass
class ApiResult:
    data: Any
    changed: bool


Fetcher = Callable[[str, dict[str, str]], Awaitable[ApiResponse]]


class WarApiError(Exception):
    pass


class WarApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        fetcher: Fetcher | None = None,
        min_interval: float = MIN_REQUEST_INTERVAL,
        timeout: float = REQUEST_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.fetcher: Fetcher = fetcher or self._http_fetch
        self.min_interval = min_interval
        self.timeout = timeout
        self.clock = clock
        self.requests = 0
        self.not_modified = 0
        self.last_error: str | None = None
        self._etags: dict[str, str] = {}
        self._cache: dict[str, Any] = {}
        self._session: aiohttp.ClientSession | None = None
        self._pace_lock = asyncio.Lock()
        self._last_request = -math.inf

    def cached(self, path: str) -> Any:
        return self._cache.get(path)

    def forget(self, path: str | None = None) -> None:
        if path is None:
            self._etags.clear()
            self._cache.clear()
            return
        self._etags.pop(path, None)
        self._cache.pop(path, None)

    async def get(self, path: str) -> ApiResult:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        etag = self._etags.get(path)
        if etag is not None and path in self._cache:
            headers["If-None-Match"] = etag
        await self._pace()
        try:
            response = await asyncio.wait_for(self.fetcher(self.base_url + path, headers), self.timeout + CONNECT_TIMEOUT)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            self.last_error = f"{path}: {type(error).__name__}"
            raise WarApiError(f"{path}: {type(error).__name__}: {error}") from error
        self.requests += 1
        if response.status == 304:
            if path in self._cache:
                self.not_modified += 1
                self.last_error = None
                return ApiResult(self._cache[path], False)
            self._etags.pop(path, None)
            self.last_error = f"{path}: 304 without cached data"
            raise WarApiError(f"{path}: 304 Not Modified without cached data")
        if not 200 <= response.status < 300:
            self.last_error = f"{path}: HTTP {response.status}"
            raise WarApiError(f"{path}: HTTP {response.status}")
        if response.data is None:
            self.last_error = f"{path}: empty response"
            raise WarApiError(f"{path}: empty response")
        self._cache[path] = response.data
        if response.etag:
            self._etags[path] = response.etag
        else:
            self._etags.pop(path, None)
        self.last_error = None
        return ApiResult(response.data, True)

    async def _pace(self) -> None:
        async with self._pace_lock:
            wait = self._last_request + self.min_interval - self.clock()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = self.clock()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=CONNECT_TIMEOUT),
            )
        return self._session

    async def _http_fetch(self, url: str, headers: dict[str, str]) -> ApiResponse:
        session = self._ensure_session()
        async with session.get(url, headers=headers) as response:
            etag = response.headers.get("ETag")
            if response.status == 304:
                return ApiResponse(304, None, etag)
            if response.status >= 400:
                return ApiResponse(response.status)
            data = await response.json(content_type=None)
            return ApiResponse(response.status, data, etag)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
