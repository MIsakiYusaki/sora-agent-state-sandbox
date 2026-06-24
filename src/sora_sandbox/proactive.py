from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProactiveTrigger:
    trigger_type: str
    idle_seconds: int
    fired_at: int
    simulated: bool = False

    def to_dict(self) -> dict:
        return {
            "type": self.trigger_type,
            "idle_seconds": self.idle_seconds,
            "fired_at": self.fired_at,
            "simulated": self.simulated,
        }


class ProactiveScheduler:
    """Small REPL scheduler that emits one trigger after an idle threshold."""

    def __init__(
        self,
        callback: Callable[[ProactiveTrigger], None],
        *,
        idle_seconds: int = 180,
        poll_seconds: float = 1.0,
    ) -> None:
        self.callback = callback
        self.idle_seconds = idle_seconds
        self.poll_seconds = poll_seconds
        self._last_activity = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._armed = True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="sora-proactive-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.poll_seconds * 2))

    def mark_activity(self) -> None:
        self._last_activity = time.monotonic()
        self._armed = True

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            idle = int(time.monotonic() - self._last_activity)
            if not self._armed or idle < self.idle_seconds:
                continue
            self._armed = False
            self.callback(
                ProactiveTrigger(
                    trigger_type="idle_timer",
                    idle_seconds=idle,
                    fired_at=int(time.time() * 1000),
                )
            )
