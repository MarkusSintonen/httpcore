import asyncio
import os
import time
from contextlib import contextmanager
from typing import Any, Coroutine, Iterator

import aiohttp
import matplotlib.pyplot as plt
import pyinstrument
from matplotlib.axes import Axes

import httpcore

PORT = 1234
URL = f"http://localhost:{PORT}/req"
REPEATS = 10
REQUESTS = 500
CONCURRENCY = 20
POOL_LIMIT = 100
PROFILE = False
os.environ["HTTPCORE_PREFER_ANYIO"] = "0"


def duration(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


@contextmanager
def profile():
    if not PROFILE:
        yield
        return
    with pyinstrument.Profiler() as profiler:
        yield
    profiler.open_in_browser()


async def gather_limited_concurrency(
    coros: Iterator[Coroutine[Any, Any, Any]], concurrency: int = CONCURRENCY
) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def coro_with_sem(coro: Coroutine[Any, Any, Any]) -> None:
        async with sem:
            await coro

    await asyncio.gather(*(coro_with_sem(c) for c in coros))


async def run_requests(axis: Axes) -> None:
    async def httpcore_get(
        pool: httpcore.AsyncConnectionPool, timings: list[int]
    ) -> None:
        start = time.monotonic()
        res = await pool.request("GET", URL)
        assert len(await res.aread()) == 2000
        assert res.status == 200, f"status_code={res.status}"
        timings.append(duration(start))

    async def aiohttp_get(session: aiohttp.ClientSession, timings: list[int]) -> None:
        start = time.monotonic()
        async with session.request("GET", URL) as res:
            assert len(await res.read()) == 2000
            assert res.status == 200, f"status={res.status}"
        timings.append(duration(start))

    async with httpcore.AsyncConnectionPool(max_connections=POOL_LIMIT) as pool:
        # warmup
        await gather_limited_concurrency(
            (httpcore_get(pool, []) for _ in range(REQUESTS)), CONCURRENCY * 2
        )

        timings: list[int] = []
        start = time.monotonic()
        with profile():
            for _ in range(REPEATS):
                await gather_limited_concurrency(
                    (httpcore_get(pool, timings) for _ in range(REQUESTS))
                )
        axis.plot(
            [*range(len(timings))], timings, label=f"httpcore (tot={duration(start)}ms)"
        )

    connector = aiohttp.TCPConnector(limit=POOL_LIMIT)
    async with aiohttp.ClientSession(connector=connector) as session:
        # warmup
        await gather_limited_concurrency(
            (aiohttp_get(session, []) for _ in range(REQUESTS)), CONCURRENCY * 2
        )

        timings = []
        start = time.monotonic()
        for _ in range(REPEATS):
            await gather_limited_concurrency(
                (aiohttp_get(session, timings) for _ in range(REQUESTS))
            )
        axis.plot(
            [*range(len(timings))], timings, label=f"aiohttp (tot={duration(start)}ms)"
        )


def main() -> None:
    fig, ax = plt.subplots()
    asyncio.run(run_requests(ax))
    plt.legend(loc="upper left")
    ax.set_xlabel("# request")
    ax.set_ylabel("[ms]")
    plt.show()
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
