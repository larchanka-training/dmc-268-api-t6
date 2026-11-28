"""Due dates of the loans."""

from datetime import date, timedelta

from .records import LOANS


def extend_due_date(loan_id: str) -> date:
    loan = LOANS[loan_id]
    return loan.due + timedelta(days=7)
