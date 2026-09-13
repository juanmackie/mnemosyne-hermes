"""A tiny single-worker background queue.

Hermes requires sync_turn to be non-blocking. The MemoryManager already runs
provider writes on its own executor, but this adapter is also usable standalone
and must never let a slow MCP round-trip stall a turn, so every capture and
warm-up goes through this worker.

One worker thread also serializes writes (turn N before turn N+1), which keeps
the Rust side's turn ordering deterministic.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STOP = object()


class _Barrier:
    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


class BackgroundWorker:
    """Serialized daemon worker with a drain barrier."""

    def __init__(self, name: str = "mnemosyne-rust-worker", max_failures: int = 50) -> None:
        self._name = name
        self._queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stopped = False
        self._max_failures = max_failures
        self.failures: List[Tuple[str, str]] = []
        self.processed = 0
        self.submitted = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopped = False
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True
            )
            self._thread.start()

    def submit(self, fn: Callable[[], None], kind: str = "task") -> bool:
        """Queue fn; returns False once the worker is stopping."""
        with self._lock:
            if self._stopped:
                return False
        self._queue.put((fn, kind))
        self.submitted += 1
        return True

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Block until every previously submitted task finished.

        Returns True on success and False on timeout or a stopped worker.
        """
        barrier = _Barrier()
        if not self.submit(barrier.event.set, kind="barrier"):
            return True
        if not barrier.event.wait(timeout):
            logger.warning("mnemosyne-rust: background worker drain timed out")
            return False
        return True

    def stop(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            self._stopped = True
        self._queue.put((_STOP, "stop"))
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP or item[0] is _STOP:
                return
            fn, kind = item
            try:
                fn()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("mnemosyne-rust: background task %s failed: %s", kind, exc)
                self.failures.append((kind, str(exc)))
                if len(self.failures) > self._max_failures:
                    del self.failures[0 : len(self.failures) - self._max_failures]
            finally:
                self.processed += 1

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()
