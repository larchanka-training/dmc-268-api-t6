"""Workspace status enumeration."""

from __future__ import annotations

from enum import StrEnum


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"
