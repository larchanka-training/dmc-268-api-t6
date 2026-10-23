"""Mounts every HTTP router of the billing API."""

from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter(prefix="/api")
