from collections.abc import Callable


class PublishReceipt:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted


def was_accepted(receipt: PublishReceipt) -> bool:
    return receipt.accepted


class JobStore:
    def __init__(self) -> None:
        self._ready: list[str] = []

    def add_ready(self, job_id: str) -> None:
        self._ready.append(job_id)

    @property
    def ready(self) -> tuple[str, ...]:
        return tuple(self._ready)

    def mark_dispatched(self, batch: tuple[str, ...]) -> None:
        for job_id in batch:
            self._ready.remove(job_id)


class DispatchService:
    def __init__(self, store: JobStore) -> None:
        self._store = store

    def flush(self, publish: Callable[[tuple[str, ...]], PublishReceipt]) -> bool:
        batch = self._store.ready
        if not batch:
            return True
        accepted = was_accepted(publish(batch))
        if accepted:
            self._store.mark_dispatched(batch)
        return accepted
