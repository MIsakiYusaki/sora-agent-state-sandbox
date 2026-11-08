from __future__ import annotations

import threading
import time
import uuid
import logging
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import CancellationToken, TurnStatus
from .journal import ConversationJournal


logger = logging.getLogger(__name__)


class ConversationBusy(RuntimeError):
    pass


@dataclass
class ActiveExecution:
    conversation_id: str
    turn_id: str
    owner_id: str
    cancellation: CancellationToken
    future: Future[Any] | None = None


class TurnCoordinator:
    """Owns bounded workers and the one-execution-per-conversation invariant."""

    def __init__(
        self,
        journal: ConversationJournal,
        *,
        max_workers: int = 4,
        lease_ttl_ms: int = 120_000,
        preemption_wait_ms: int = 2_000,
    ) -> None:
        self.journal = journal
        self.lease_ttl_ms = max(5_000, lease_ttl_ms)
        self.preemption_wait_ms = max(0, preemption_wait_ms)
        self.owner_id = f"runtime_{uuid.uuid4().hex}"
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="sora-turn"
        )
        self._lock = threading.RLock()
        self._active: dict[str, ActiveExecution] = {}

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            active = tuple(self._active.values())
        for execution in active:
            execution.cancellation.cancel("runtime_shutdown")
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def submit(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        operation: Callable[[CancellationToken], Any],
    ) -> Future[Any]:
        execution = self._claim(conversation_id, turn_id)

        def run() -> Any:
            try:
                return self._run_with_heartbeat(execution, operation)
            finally:
                self._release(execution)

        future = self._executor.submit(run)
        execution.future = future
        return future

    def run(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        operation: Callable[[CancellationToken], Any],
    ) -> Any:
        execution = self._claim(conversation_id, turn_id)
        try:
            return self._run_with_heartbeat(execution, operation)
        finally:
            self._release(execution)

    def cancel(
        self, conversation_id: str, *, reason: str = "user_superseded",
        turn_id: str | None = None,
    ) -> bool:
        with self._lock:
            execution = self._active.get(conversation_id)
            if execution is None or (turn_id is not None and execution.turn_id != turn_id):
                return False
            # Match and signal the same execution while claim/release is fenced.
            changed = execution.cancellation.cancel(reason)
        if changed:
            try:
                context = self.journal.get_turn(execution.turn_id)
                if context.status not in {
                    TurnStatus.COMMITTING,
                    TurnStatus.COMMITTED,
                    TurnStatus.CANCELLED,
                    TurnStatus.FAILED,
                }:
                    self.journal.transition(
                        execution.turn_id,
                        TurnStatus.CANCEL_REQUESTED,
                        stage="cancel_requested",
                    )
            except (KeyError, RuntimeError):
                pass
        return changed

    def _claim(self, conversation_id: str, turn_id: str) -> ActiveExecution:
        previous: ActiveExecution | None
        with self._lock:
            previous = self._active.get(conversation_id)
        if previous is not None and previous.turn_id != turn_id:
            previous.cancellation.cancel("superseded_by_new_user_turn")
            if previous.future is not None and self.preemption_wait_ms:
                try:
                    previous.future.result(timeout=self.preemption_wait_ms / 1000)
                except FutureTimeoutError:
                    logger.warning(
                        "turn %s did not stop within the %sms preemption window",
                        previous.turn_id,
                        self.preemption_wait_ms,
                    )
                except Exception:
                    pass
        execution = ActiveExecution(
            conversation_id=conversation_id,
            turn_id=turn_id,
            owner_id=self.owner_id,
            cancellation=CancellationToken(),
        )
        with self._lock:
            current = self._active.get(conversation_id)
            if current is not None and current.turn_id != turn_id:
                raise ConversationBusy(f"conversation already executing: {conversation_id}")
            if not self.journal.acquire_lease(
                conversation_id,
                turn_id,
                self.owner_id,
                ttl_ms=self.lease_ttl_ms,
            ):
                raise ConversationBusy(f"conversation lease is held elsewhere: {conversation_id}")
            self._active[conversation_id] = execution
        return execution

    def _release(self, execution: ActiveExecution) -> None:
        self.journal.release_lease(
            execution.conversation_id, execution.turn_id, execution.owner_id
        )
        with self._lock:
            if self._active.get(execution.conversation_id) is execution:
                self._active.pop(execution.conversation_id, None)

    def _run_with_heartbeat(
        self,
        execution: ActiveExecution,
        operation: Callable[[CancellationToken], Any],
    ) -> Any:
        stop = threading.Event()

        def heartbeat() -> None:
            interval = max(1.0, self.lease_ttl_ms / 3000)
            while not stop.wait(interval):
                renewed = self.journal.renew_lease(
                    execution.conversation_id,
                    execution.turn_id,
                    execution.owner_id,
                    ttl_ms=self.lease_ttl_ms,
                )
                if not renewed:
                    execution.cancellation.cancel("execution_lease_lost")
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"sora-lease-{execution.turn_id[-8:]}",
            daemon=True,
        )
        thread.start()
        try:
            return operation(execution.cancellation)
        finally:
            stop.set()
            thread.join(timeout=1.0)
