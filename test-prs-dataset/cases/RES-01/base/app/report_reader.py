from pathlib import Path


def read_report(path: Path) -> str:
    with path.open(encoding="utf-8") as report:
        return report.read()
