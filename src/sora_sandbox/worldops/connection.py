"""Nested canonical WorldOps writes with transaction-local read visibility."""
import sqlite3
import threading


class WorldOpsConnection(sqlite3.Connection):
    _depth = 0
    _rollback_only = False
    _writer_thread = None

    @property
    def owns_transaction(self) -> bool:
        return self._depth > 0 and self._writer_thread == threading.get_ident()

    def __enter__(self):
        if not self._depth:
            self._rollback_only = False
            self._writer_thread = threading.get_ident()
            if not self.in_transaction:
                self.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self

    def __exit__(self, kind, value, traceback):
        self._depth -= 1
        if kind is not None:
            self._rollback_only = True
            super().rollback()
        if self._depth == 0:
            try:
                if self._rollback_only:
                    super().rollback()
                    if kind is None:
                        raise sqlite3.IntegrityError("nested WorldOps transaction was rolled back")
                else:
                    try:
                        super().commit()
                    except BaseException:
                        super().rollback()
                        raise
            finally:
                self._writer_thread = None
        return False
