"""Per-user upload quota."""

QUOTA_BYTES = 50 * 1024 * 1024


def has_room(used_bytes: int, new_bytes: int) -> bool:
    return used_bytes + new_bytes <= QUOTA_BYTES
