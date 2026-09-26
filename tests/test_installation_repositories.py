"""Public contracts for installation repository snapshots and tree language shares."""

from __future__ import annotations

import pytest

from app.modules.repositories.application.installation_repositories import (
    InstallationEventValidationError,
    RepositoryTreeBlob,
    classify_tree_languages,
    parse_installation_repositories_event,
)


def test_parse_created_installation_snapshot_as_added_repositories() -> None:
    event = parse_installation_repositories_event(
        event_name="installation",
        payload={
            "action": "created",
            "installation": {"id": 17},
            "repositories": [
                {
                    "id": 101,
                    "full_name": "octo/api",
                    "default_branch": "main",
                    "html_url": "https://github.com/octo/api",
                }
            ],
        },
    )

    assert event.installation_external_id == 17
    assert event.added_repositories[0].external_id == 101
    assert event.added_repositories[0].full_name == "octo/api"
    assert event.added_repositories[0].default_branch == "main"
    assert event.removed_repositories == ()


def test_parse_added_and_removed_repository_snapshots_from_github_event() -> None:
    added = parse_installation_repositories_event(
        event_name="installation_repositories",
        payload={
            "action": "added",
            "installation": {"id": 17},
            "repositories_added": [
                {
                    "id": 102,
                    "full_name": "octo/web",
                    "default_branch": "trunk",
                    "html_url": "https://github.com/octo/web",
                }
            ],
        },
    )
    removed = parse_installation_repositories_event(
        event_name="installation_repositories",
        payload={
            "action": "removed",
            "installation": {"id": 17},
            "repositories_removed": [
                {
                    "id": 102,
                    "full_name": "octo/web",
                    "default_branch": "trunk",
                    "html_url": "https://github.com/octo/web",
                }
            ],
        },
    )

    assert [repository.external_id for repository in added.added_repositories] == [102]
    assert removed.added_repositories == ()
    assert [repository.external_id for repository in removed.removed_repositories] == [102]


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("push", {"action": "created", "installation": {"id": 17}}),
        ("installation", {"action": "suspend", "installation": {"id": 17}}),
        ("installation_repositories", {"action": "added", "installation": {"id": "17"}}),
    ],
)
def test_parse_rejects_unsupported_or_malformed_installation_events(
    event_name: str, payload: dict[str, object]
) -> None:
    with pytest.raises(InstallationEventValidationError):
        parse_installation_repositories_event(event_name=event_name, payload=payload)


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        (
            "installation",
            {
                "action": "created",
                "installation": {"id": 17},
            },
        ),
        (
            "installation_repositories",
            {
                "action": "added",
                "installation": {"id": 17},
                "repositories_removed": [],
            },
        ),
        (
            "installation_repositories",
            {
                "action": "removed",
                "installation": {"id": 17},
                "repositories_added": [],
            },
        ),
    ],
)
def test_parse_requires_the_repository_array_for_each_event_action(
    event_name: str, payload: dict[str, object]
) -> None:
    with pytest.raises(
        InstallationEventValidationError,
        match="invalid installation repository payload",
    ):
        parse_installation_repositories_event(event_name=event_name, payload=payload)


def test_classify_tree_languages_is_deterministic_and_uses_blob_sizes() -> None:
    tree = (
        RepositoryTreeBlob(path="README.md", size=100, entry_type="blob"),
        RepositoryTreeBlob(path="web/app.ts", size=2, entry_type="blob"),
        RepositoryTreeBlob(path="api/main.py", size=1, entry_type="blob"),
    )

    languages = classify_tree_languages(tuple(reversed(tree)))

    assert languages == {"Python": 33, "TypeScript": 67}


def test_classify_tree_languages_ignores_unknown_and_empty_blobs() -> None:
    languages = classify_tree_languages(
        (
            RepositoryTreeBlob(path="README.md", size=100, entry_type="blob"),
            RepositoryTreeBlob(path="api/empty.py", size=0, entry_type="blob"),
        )
    )

    assert languages == {}


def test_classify_tree_languages_ignores_non_blob_entries() -> None:
    languages = classify_tree_languages(
        (
            RepositoryTreeBlob(path="api/main.py", size=120, entry_type="blob"),
            RepositoryTreeBlob(path="vendor/generated.py", size=900, entry_type="tree"),
            RepositoryTreeBlob(path="submodule/client.ts", size=600, entry_type="commit"),
        )
    )

    assert languages == {"Python": 100}
