from __future__ import annotations

import itertools
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable


WriteCallable = Callable[[], Any]


@dataclass(order=True)
class _WriteRequest:
    priority: int
    sequence: int
    operation: WriteCallable = field(compare=False)
    future: Future[Any] = field(compare=False)
    name: str = field(compare=False, default="write")


class PersonaWriteCoordinator:
    """Serializes SQLite writes and applies priority-aware backpressure."""

    def __init__(self, *, high_watermark: int = 500, max_queue: int = 1000) -> None:
        if not 0 < high_watermark <= max_queue:
            raise ValueError("invalid Persona write queue watermarks")
        self.high_watermark = high_watermark
        self.max_queue = max_queue
        self._queue: queue.PriorityQueue[_WriteRequest | tuple[int, int, None]] = queue.PriorityQueue(maxsize=max_queue)
        self._sequence = itertools.count()
        self._closed = threading.Event()
        self._worker = threading.Thread(target=self._run, name="persona-write-coordinator", daemon=True)
        self._worker.start()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def under_backpressure(self) -> bool:
        return self.queue_depth >= self.high_watermark

    def submit(
        self,
        operation: WriteCallable,
        *,
        priority: int,
        name: str,
        timeout: float = 10.0,
    ) -> Any:
        if self._closed.is_set():
            raise RuntimeError("Persona write coordinator is closed")
        if self.under_backpressure and priority >= 3:
            raise RuntimeError("Persona write queue backpressure rejected low-priority work")
        future: Future[Any] = Future()
        request = _WriteRequest(priority, next(self._sequence), operation, future, name)
        try:
            self._queue.put(request, timeout=min(timeout, 1.0))
        except queue.Full as exc:
            raise RuntimeError("Persona write queue is full") from exc
        return future.result(timeout=timeout)

    def close(self, *, timeout: float = 10.0) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put((-1, next(self._sequence), None))
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            raise RuntimeError("Persona write coordinator did not stop cleanly")

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            try:
                if isinstance(request, tuple):
                    return
                if request.future.set_running_or_notify_cancel():
                    try:
                        request.future.set_result(request.operation())
                    except BaseException as exc:
                        request.future.set_exception(exc)
            finally:
                self._queue.task_done()
