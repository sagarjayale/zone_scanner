"""Scans run in separate worker processes on MySQL, so several stocks are scanned at the same time on several CPU cores,
and the web server's own process stays free to answer the pages (a scan in a thread of the same process competes with
the pages for Python's one interpreter lock).

Workers: SCAN_WORKERS in .env (at most 4), or by default 2 on a computer with 6 or more CPU cores, else 1. They run at
a lower CPU priority (nice 10) than the web server, so a page request is served first. SCAN_WORKERS=0 switches
the workers off (scans then run one at a time in a thread, as before). On SQLite (and in the tests) scans always run
in a thread: SQLite lets only one writer in at a time, so processes would only wait for each other.
One stock is never scanned twice at the same time: a second request for the same stock waits for the first.

Each worker opens its own database connections (app/db.py is imported fresh in the worker), reads the same .env, and
writes only the rows of the stock it scans. The scan-date checks of the Backtest page get their own workers, so the
live scans never wait behind a long check."""
import asyncio
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime

from ..db import IS_SQLITE
from ..engine import scanner

log = logging.getLogger(__name__)
_pool: ProcessPoolExecutor | None = None
_heavy: ProcessPoolExecutor | None = None
_locks: dict[str, asyncio.Lock] = {}
MAX_WORKERS = 4


def workers() -> int:
    """How many worker processes scans use (0: scans run in a thread of the web server)."""
    if IS_SQLITE:
        return 0
    env = os.getenv("SCAN_WORKERS")
    if env not in (None, ""):
        try:
            return max(0, min(MAX_WORKERS, int(env)))
        except ValueError:
            log.warning("SCAN_WORKERS=%r is not a number; using the default", env)
    return 2 if (os.cpu_count() or 2) >= 6 else 1


def _low_priority() -> None:
    """Each worker runs at a lower CPU priority than the web server, so a page request is always served first."""
    try:
        os.nice(10)
    except (AttributeError, OSError):      # not on Linux, or not allowed: run at normal priority
        pass


def _new_pool() -> ProcessPoolExecutor:
    # "spawn" starts each worker as a clean Python process: it never inherits the web server's threads, sockets or
    # database connections. The workers start once and are reused.
    return ProcessPoolExecutor(max_workers=workers(), mp_context=multiprocessing.get_context("spawn"), initializer=_low_priority)


async def call(key: str, fn, *args):
    """fn(*args) for one stock in a worker process (or a thread when there are no workers). Work on the same stock
    never overlaps: a second call waits for the first. fn must open its own database session."""
    global _pool
    async with _locks.setdefault(key, asyncio.Lock()):
        if workers() == 0:
            return await asyncio.to_thread(fn, *args)
        if _pool is None:
            _pool = _new_pool()
        try:
            return await asyncio.get_running_loop().run_in_executor(_pool, fn, *args)
        except BrokenProcessPool:          # a worker died (out of memory, killed): start fresh ones, do this one here
            log.warning("scan workers stopped unexpectedly; starting new ones")
            _pool = None
            return await asyncio.to_thread(fn, *args)


async def scan(key: str, force: bool = True, now: datetime | None = None, timeframes: list[str] | None = None) -> list[dict]:
    """scanner.scan_key for one stock, in a worker process (or a thread when there are no workers)."""
    return await call(key, scanner.scan_key, key, force, now, timeframes)


async def map_heavy(fn, calls: list[tuple]) -> list:
    """fn(*args) for every args in calls, several at once in worker processes (results in the same order), or one
    after another in a thread when there are no workers. fn must open its own database session."""
    global _heavy
    if workers() == 0:
        return await asyncio.to_thread(lambda: [fn(*a) for a in calls])
    if _heavy is None:
        _heavy = _new_pool()
    loop = asyncio.get_running_loop()
    try:
        return list(await asyncio.gather(*(loop.run_in_executor(_heavy, fn, *a) for a in calls)))
    except BrokenProcessPool:
        log.warning("check workers stopped unexpectedly; finishing in a thread")
        _heavy = None
        return await asyncio.to_thread(lambda: [fn(*a) for a in calls])


def shutdown() -> None:
    global _pool, _heavy
    for p in (_pool, _heavy):
        if p is not None:
            p.shutdown(wait=False, cancel_futures=True)
    _pool = _heavy = None
