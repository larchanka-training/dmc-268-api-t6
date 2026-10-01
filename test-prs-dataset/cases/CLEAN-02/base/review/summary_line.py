from __future__ import annotations


class SummaryLine:
    def render(self, title: str, score: float | None) -> str:
        if score is None:
            return title
        return f"{title}: {score:.1f}%"


class RunSummaryLine(SummaryLine):
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id

    def render(self, title: str, score: float | None) -> str:
        body = super(RunSummaryLine, self).render(title, score)
        return f"[run {self.run_id}] {body}"
