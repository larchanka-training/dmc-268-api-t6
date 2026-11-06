"""The anonymous badge route: outside ``api_router``, so no Bearer is asked for (#454)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.bootstrap.reviews_api import get_repository_badge
from app.modules.reviews.application.repository_badge import BadgeStatus, GetRepositoryBadge

# Recorded in contracts/openapi.yaml: no cache sits on the application side, so a new verdict
# reaches the README through GitHub's image proxy within this time.
BADGE_CACHE_CONTROL = "public, max-age=300"

_LABEL_WIDTH = 62
_VALUE_STYLES: dict[BadgeStatus, tuple[int, str]] = {
    BadgeStatus.PASSED: (50, "#1a7f37"),
    BadgeStatus.ATTENTION: (62, "#9a6700"),
    BadgeStatus.BLOCKING: (58, "#cf222e"),
    BadgeStatus.NO_DATA: (56, "#6e7781"),
}


def _svg(status: BadgeStatus) -> bytes:
    """Built only from the constants above: nothing of the request reaches the markup."""
    width, color = _VALUE_STYLES[status]
    alternative = f"AI review: {status.value}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_LABEL_WIDTH + width}" height="20" '
        f'role="img" aria-label="{alternative}"><title>{alternative}</title>'
        f'<rect width="{_LABEL_WIDTH}" height="20" fill="#24292f"/>'
        f'<rect x="{_LABEL_WIDTH}" width="{width}" height="20" fill="{color}"/>'
        '<g fill="#ffffff" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11" '
        'text-anchor="middle">'
        f'<text x="{_LABEL_WIDTH // 2}" y="14">AI review</text>'
        f'<text x="{_LABEL_WIDTH + width // 2}" y="14">{status.value}</text>'
        "</g></svg>"
    ).encode()


_BADGES = {status: _svg(status) for status in BadgeStatus}

badge_router = APIRouter(prefix="/api/badges", tags=["badges"])


@badge_router.get(
    "/github/{owner}/{repo}.svg",
    response_class=Response,
    responses={
        200: {
            "description": "The badge; the same neutral one for every case without a status",
            "content": {"image/svg+xml": {}},
        }
    },
)
async def get_github_repository_badge(
    owner: str,
    repo: str,
    use_case: Annotated[GetRepositoryBadge, Depends(get_repository_badge)],
) -> Response:
    status = await use_case.execute(owner, repo)
    return Response(
        content=_BADGES[status],
        media_type="image/svg+xml",
        headers={"Cache-Control": BADGE_CACHE_CONTROL},
    )
