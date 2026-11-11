"""Loan records of the library."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Loan:
    member: str
    isbn: str
    due: date


LOANS: dict[str, Loan] = {
    "loan-1": Loan("m-104", "9780141036144", date(2026, 3, 12)),
    "loan-2": Loan("m-231", "9780140449136", date(2026, 3, 20)),
}
