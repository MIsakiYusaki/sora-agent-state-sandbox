"""Nested owner transactions and a strictly post-commit delivery notification."""
from __future__ import annotations

import sqlite3
from typing import Callable


class MemoryConnection(sqlite3.Connection):
    """Keep legacy helper commits inside the outer Memory unit of work.

    The owner holds its RLock for every context. A failed nested context makes
    the whole unit rollback-only; catching its exception cannot commit a subset.
    Only a successful outer commit may notify derived-state delivery.
    """

    after_commit: Callable[[], None] | None = None
    after_rollback: Callable[[], None] | None = None
    _depth = 0
    _rollback_only = False
    _notifying = False

    def __enter__(self):
        if self._depth == 0:
            self._rollback_only = False
            if not self.in_transaction:
                self.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self

    def __exit__(self, kind, value, traceback):
        self._depth -= 1
        if kind is not None:
            self._rollback_only = True
            self.rollback()
        elif self._depth == 0:
            if self._rollback_only:
                self.rollback()
                raise sqlite3.IntegrityError("nested Memory transaction was rolled back")
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        return False

    def commit(self):
        if self._depth:
            return
        super().commit()
        if self.after_commit is not None and not self._notifying:
            self._notifying = True
            try:
                self.after_commit()
            finally:
                self._notifying = False

    def rollback(self):
        if self._depth:
            self._rollback_only = True
        super().rollback()
        if self.after_rollback is not None:
            self.after_rollback()
