from __future__ import annotations

from collections import deque
from collections.abc import Callable


class WorkerStopped(RuntimeError):
    pass


class SettlementBatchWorker:
    MAX_RETAINED_BYTES = 8 * 1024 * 1024

    def __init__(self, write_batch: Callable[[bytes], None]) -> None:
        self._write_batch = write_batch
        self._recent_batches: deque[bytes] = deque(maxlen=2)
        self._stopped = False

    @property
    def retained_bytes(self) -> int:
        return sum(len(batch) for batch in self._recent_batches)

    def process_batch(self, payload: bytes) -> None:
        if self._stopped:
            raise WorkerStopped("settlement worker stopped")
        self._recent_batches.append(payload)
        if self.retained_bytes > self.MAX_RETAINED_BYTES:
            self._stopped = True
            raise WorkerStopped("settlement worker memory budget exceeded")
        self._write_batch(payload)
