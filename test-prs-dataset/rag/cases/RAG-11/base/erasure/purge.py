"""Hard deletion of the data kept for an account."""

from .rows import drop_rows, rows_of


def purge_account(account_id: str, *, dry_run: bool = True) -> int:
    """Delete every row kept for the account and return how many were removed.

    Nothing is deleted while dry_run is True; the count is then what would be removed.
    """
    if dry_run:
        return len(rows_of(account_id))
    return drop_rows(account_id)
