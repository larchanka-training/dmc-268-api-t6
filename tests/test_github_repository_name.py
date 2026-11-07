"""The one check of a GitHub ``owner/repo`` name shared by its users."""

from __future__ import annotations

import pytest

from app.common.application.github_repository_name import (
    REPOSITORY_FULL_NAME_MAX_LENGTH,
    is_github_repository_full_name,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("octo/repo", True, id="plain"),
        pytest.param("octo/.github", True, id="repo-starting-with-a-dot"),
        pytest.param("octo/..", False, id="dots-only"),
        pytest.param("octo", False, id="no-slash"),
        pytest.param("octo/repo\n", False, id="trailing-newline"),
        pytest.param("o/" + "a" * (REPOSITORY_FULL_NAME_MAX_LENGTH - 2), True, id="longest"),
        pytest.param("o/" + "a" * (REPOSITORY_FULL_NAME_MAX_LENGTH - 1), False, id="too-long"),
    ],
)
def test_a_name_github_could_have(value: str, expected: bool) -> None:
    assert is_github_repository_full_name(value) is expected
