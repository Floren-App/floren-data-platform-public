"""Write to the platform's central log from a run, app or Airflow deployment.

    import fdp.log
    fdp.log.info("loaded rows", table="sales", rows=120)
    fdp.log.warning("3 rows without a region")
    fdp.log.error("upload failed", attempt=2)

    # or route Python's own logging there:
    import logging
    logging.getLogger().addHandler(fdp.log.handler())

Each line also prints to the console. The platform stamps the run, workload, code version (commit) and
layer; lines appear under the workload, the run and in the Workloads log. Sending happens in the
background, in batches, and never raises: a logging outage does not fail your program.
Outside Floren Data Platform (no FDP_API), lines only print.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = ["debug", "info", "warning", "error", "exception", "event", "flush", "handler", "FDPLogHandler"]

_BATCH = 50
_INTERVAL = 1.0
_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=10_000)
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_flushed = threading.Event()


def _enabled() -> bool:
    return bool(os.environ.get("FDP_API") and (os.environ.get("FDP_TOKEN") or os.environ.get("FDP_SESSION_TOKEN")))


def _send(entries: List[Dict[str, Any]]) -> None:
    from . import _client
    try:
        with _client() as client:
            client.post("/v1/logs", json={"entries": entries}, timeout=10)
    except Exception:  # never fail the program over its log
        pass


def _worker() -> None:
    while True:
        batch: List[Dict[str, Any]] = []
        try:
            batch.append(_queue.get(timeout=_INTERVAL))
            while len(batch) < _BATCH:
                batch.append(_queue.get_nowait())
        except queue.Empty:
            pass
        if batch:
            _send(batch)
        if _queue.empty():
            _flushed.set()


def _start() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="fdp-log", daemon=True)
            _thread.start()


def _write(level: str, message: str, fields: Dict[str, Any], event: str = "") -> None:
    message = str(message)
    suffix = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{level.upper()}] {message}{' ' + suffix if suffix else ''}", file=sys.stderr, flush=True)
    if not _enabled():
        return
    entry: Dict[str, Any] = {"level": level, "message": message[:4000], "at": datetime.now(timezone.utc).isoformat()}
    if fields:
        entry["fields"] = {k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v)) for k, v in fields.items()}
    if event:
        entry["event"] = event
    _flushed.clear()
    try:
        _queue.put_nowait(entry)
    except queue.Full:
        return  # a program logging faster than it can send keeps running; the newest lines are dropped
    _start()


def debug(message: str, **fields: Any) -> None:
    _write("debug", message, fields)


def info(message: str, **fields: Any) -> None:
    _write("info", message, fields)


def warning(message: str, **fields: Any) -> None:
    _write("warning", message, fields)


def error(message: str, **fields: Any) -> None:
    _write("error", message, fields)


def exception(message: str, **fields: Any) -> None:
    """An error with the current exception's traceback (call it in an except block)."""
    import traceback
    _write("error", message, {**fields, "traceback": traceback.format_exc()[-3000:]})


def event(name: str, message: str, level: str = "info", **fields: Any) -> None:
    """A named event (e.g. dag.started), used by the Airflow listener."""
    _write(level, message, fields, event=name)


def flush(timeout: float = 5.0) -> None:
    """Wait until queued lines are sent (called automatically at exit)."""
    if _thread is not None and _thread.is_alive() and not _queue.empty():
        _flushed.wait(timeout)
        deadline = time.monotonic() + timeout
        while not _queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)


atexit.register(flush)

_LEVELS = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error"}


class FDPLogHandler(logging.Handler):
    """A logging.Handler that sends records to the central log (it does not print them again)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = _LEVELS.get(record.levelno, "error" if record.levelno > logging.ERROR else "info")
            if not _enabled() or record.name.startswith(("httpx", "httpcore")):
                return
            fields: Dict[str, Any] = {"logger": record.name}
            if record.exc_info:
                fields["traceback"] = self.formatException(record.exc_info)[-3000:]
            entry = {"level": level, "message": record.getMessage()[:4000], "fields": fields,
                     "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat()}
            _flushed.clear()
            _queue.put_nowait(entry)
            _start()
        except Exception:
            pass


def handler(level: int = logging.INFO) -> FDPLogHandler:
    h = FDPLogHandler()
    h.setLevel(level)
    return h
