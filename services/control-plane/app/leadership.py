"""Several control plane replicas: every replica serves requests, one leader runs the background work.

Leadership is a PostgreSQL session advisory lock held on a dedicated connection to the metadata database. If the
leader stops, crashes or loses its connection, PostgreSQL releases the lock (TCP keepalives detect a dead peer in
about half a minute) and another replica takes over at its next attempt. No Kubernetes permissions are needed
and it behaves the same outside Kubernetes. A replica that can no longer confirm its lock stops its background
work at once. Single-process databases (SQLite in tests and development) have one replica: it always leads.

The same mechanism gives cluster-wide locks for short critical sections (``cluster_lock``), e.g. one deploy per
workload at a time across replicas.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
from typing import Awaitable, Callable, Iterator, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)
LEADER = "fdp:control-plane:leader"
CHECK_SECONDS = 5
# Detect a dead connection (and so a dead leader) in about half a minute.
KEEPALIVE = {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5, "keepalives_count": 3}


def lock_key(name: str) -> int:
    """A stable signed 64-bit key for pg_try_advisory_lock."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def _postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


_engine: Optional[Engine] = None
_local_locks: dict = {}
_local_guard = threading.Lock()


def _lock_engine(engine: Engine) -> Engine:
    """A separate small pool with keepalives, so a lost peer releases its locks promptly."""
    global _engine
    if _engine is None:
        _engine = create_engine(engine.url, pool_size=2, max_overflow=8, pool_pre_ping=True, connect_args=dict(KEEPALIVE))
    return _engine


@contextlib.contextmanager
def cluster_lock(engine: Engine, name: str) -> Iterator[bool]:
    """Yields True when this replica holds the named lock (until the block ends), False when another holds it."""
    if not _postgres(engine):
        with _local_guard:
            lock = _local_locks.setdefault(name, threading.Lock())
        got = lock.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                lock.release()
        return
    key = lock_key(name)
    with _lock_engine(engine).connect() as conn:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()


class Leadership:
    """Runs the given background loops only while this replica leads."""

    def __init__(self, engine: Engine, loops: List[Callable[[], Awaitable[None]]]):
        self.engine, self.loops = engine, loops
        self.leader = False
        self._conn: Optional[Connection] = None
        self._tasks: List[asyncio.Task] = []

    def _try_acquire(self) -> bool:
        if not _postgres(self.engine):
            return True
        if self._conn is None:
            self._conn = _lock_engine(self.engine).connect()
        got = bool(self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(LEADER)}).scalar())
        self._conn.commit()
        if not got:
            self._release_connection()
        return got

    def _still_leader(self) -> bool:
        if not _postgres(self.engine):
            return True
        # A session lock lives exactly as long as its connection: if the connection still answers, we hold it.
        try:
            self._conn.execute(text("SELECT 1"))
            self._conn.commit()
            return True
        except Exception:
            return False

    def _release_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def _start(self) -> None:
        self.leader = True
        self._tasks = [asyncio.create_task(loop()) for loop in self.loops]
        log.info("this replica now leads: background work started")

    def _stop(self) -> None:
        self.leader = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        self._release_connection()
        log.warning("this replica no longer leads: background work stopped")

    async def run(self) -> None:
        while True:
            try:
                if not self.leader:
                    if await asyncio.to_thread(self._try_acquire):
                        self._start()
                elif not await asyncio.to_thread(self._still_leader):
                    self._stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("leader election")
                if self.leader:
                    self._stop()
                else:
                    self._release_connection()
            await asyncio.sleep(CHECK_SECONDS)

    def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._release_connection()   # closing the session frees the lock for another replica right away
