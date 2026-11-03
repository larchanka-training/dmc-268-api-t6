"""In-memory storage of uploaded files."""

_AGE_DAYS: dict[str, int] = {"scan-1.pdf": 3, "photo-2.png": 12, "contract-3.pdf": 45}


def list_older_than(days: int) -> list[str]:
    return [name for name, age in _AGE_DAYS.items() if age > days]


def delete(name: str) -> None:
    del _AGE_DAYS[name]
