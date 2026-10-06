"""Public contracts for installation repository snapshots and tree language shares."""

from __future__ import annotations

from typing import Any

import pytest

from app.modules.integrations.webhooks.api.installation_event_dtos import (
    InstallationEventValidationError,
    parse_installation_repositories_event,
)
from app.modules.repositories.application.installation_repositories import (
    RepositoryTreeBlob,
    classify_tree_languages,
)
from tests.github_webhook_fixtures import load_github_webhook_fixture, removal_of

_GITHUB_REPOSITORY_ITEM_KEYS = {"id", "node_id", "name", "full_name", "private"}


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


@pytest.mark.parametrize(
    ("fixture", "array"),
    [
        ("installation_created", "repositories"),
        ("installation_repositories_added", "repositories_added"),
    ],
)
def test_real_delivery_fixtures_keep_the_five_field_repository_shape(
    fixture: str, array: str
) -> None:
    """Guard: a synthetic ``default_branch`` / ``html_url`` must not creep into the fixtures."""
    items = load_github_webhook_fixture(fixture)[array]

    assert len(items) == 1
    assert set(items[0]) == _GITHUB_REPOSITORY_ITEM_KEYS


def test_parse_real_installation_created_delivery_without_branch_and_url() -> None:
    event = parse_installation_repositories_event(
        event_name="installation",
        payload=load_github_webhook_fixture("installation_created"),
    )

    assert event.action == "created"
    assert event.installation_external_id == 1000001
    assert len(event.added_repositories) == 1
    repository = event.added_repositories[0]
    assert repository.external_id == 1000004
    assert repository.full_name == "example-owner/example-repo"
    assert repository.default_branch is None
    assert repository.web_url is None
    assert event.removed_repositories == ()


def test_parse_real_installation_repositories_added_delivery_without_branch_and_url() -> None:
    event = parse_installation_repositories_event(
        event_name="installation_repositories",
        payload=load_github_webhook_fixture("installation_repositories_added"),
    )

    assert event.action == "added"
    assert event.installation_external_id == 1000001
    assert len(event.added_repositories) == 1
    repository = event.added_repositories[0]
    assert repository.external_id == 1000005
    assert repository.full_name == "example-owner/example-repo-two"
    assert repository.default_branch is None
    assert repository.web_url is None
    assert event.removed_repositories == ()


@pytest.mark.parametrize(
    ("event_name", "fixture", "action"),
    [
        ("installation", "installation_created", "deleted"),
        ("installation_repositories", "installation_repositories_added", "removed"),
    ],
)
def test_parse_removal_deliveries_in_the_real_five_field_shape(
    event_name: str, fixture: str, action: str
) -> None:
    created_or_added = load_github_webhook_fixture(fixture)
    event = parse_installation_repositories_event(
        event_name=event_name, payload=removal_of(event_name, created_or_added)
    )

    assert event.action == action
    assert event.installation_external_id == 1000001
    assert event.added_repositories == ()
    assert [repository.external_id for repository in event.removed_repositories] == [
        1000004 if event_name == "installation" else 1000005
    ]
    assert event.removed_repositories[0].default_branch is None
    assert event.removed_repositories[0].web_url is None


def test_parse_keeps_branch_and_url_when_only_one_of_them_is_present() -> None:
    payload = load_github_webhook_fixture("installation_repositories_added")
    payload["repositories_added"][0]["default_branch"] = "trunk"

    event = parse_installation_repositories_event(
        event_name="installation_repositories", payload=payload
    )

    assert event.added_repositories[0].default_branch == "trunk"
    assert event.added_repositories[0].web_url is None


@pytest.mark.parametrize("field", ["default_branch", "html_url"])
def test_parse_rejects_empty_branch_or_url_instead_of_treating_them_as_present(
    field: str,
) -> None:
    payload = load_github_webhook_fixture("installation_repositories_added")
    payload["repositories_added"][0][field] = ""

    with pytest.raises(InstallationEventValidationError):
        parse_installation_repositories_event(
            event_name="installation_repositories", payload=payload
        )


@pytest.mark.parametrize(
    ("event_name", "fixture", "removal", "array"),
    [
        ("installation", "installation_created", False, "repositories"),
        ("installation", "installation_created", True, "repositories"),
        (
            "installation_repositories",
            "installation_repositories_added",
            False,
            "repositories_added",
        ),
        (
            "installation_repositories",
            "installation_repositories_added",
            True,
            "repositories_removed",
        ),
    ],
)
@pytest.mark.parametrize(
    "damage",
    [
        {"id": None},
        {"id": 0},
        {"id": "1000004"},
        {"full_name": None},
        {"full_name": ""},
    ],
)
def test_parse_still_requires_repository_id_and_full_name(
    event_name: str, fixture: str, removal: bool, array: str, damage: dict[str, Any]
) -> None:
    """Guard, green before the fix too: ``id`` and ``full_name`` stay required."""
    payload = load_github_webhook_fixture(fixture)
    if removal:
        payload = removal_of(event_name, payload)
    for key, value in damage.items():
        if value is None:
            del payload[array][0][key]
        else:
            payload[array][0][key] = value

    with pytest.raises(
        InstallationEventValidationError, match="invalid installation repository payload"
    ):
        parse_installation_repositories_event(event_name=event_name, payload=payload)


_EVENT_SHAPES = [
    ("installation", "installation_created", False, "repositories"),
    ("installation", "installation_created", True, "repositories"),
    ("installation_repositories", "installation_repositories_added", False, "repositories_added"),
    ("installation_repositories", "installation_repositories_added", True, "repositories_removed"),
]


@pytest.mark.parametrize(("event_name", "fixture", "removal", "array"), _EVENT_SHAPES)
@pytest.mark.parametrize(
    "full_name",
    [
        "o/..",
        "../r",
        "o/.",
        "o/r/x",
        "o/",
        "/r",
        "o /r",
        "o/r p",
        "o/r\n",
        "o/r\x00",
        "o/r\t",
        "o.x/r",
        "o/...",
        "o/%2e%2e",
        "o/r\u0661",
    ],
)
def test_parse_rejects_repository_names_outside_owner_slash_repo(
    event_name: str, fixture: str, removal: bool, array: str, full_name: str
) -> None:
    """``full_name`` ends up in a request path carrying the installation token."""
    payload = load_github_webhook_fixture(fixture)
    if removal:
        payload = removal_of(event_name, payload)
    payload[array][0]["full_name"] = full_name

    with pytest.raises(InstallationEventValidationError) as raised:
        parse_installation_repositories_event(event_name=event_name, payload=payload)

    assert [location for location, _ in raised.value.field_errors] == [f"{array}.0.full_name"]
    assert "should match pattern" in raised.value.field_errors[0][1]


@pytest.mark.parametrize(
    "full_name",
    [
        "example-owner/example-repo",
        "example-owner/example-repo-two",
        "octo/.github",
        "octo_acme/repo",
        "owner/repo.js",
        "o/r_",
        "o/-x",
        "o/r.",
    ],
)
def test_parse_accepts_real_github_repository_names(full_name: str) -> None:
    payload = load_github_webhook_fixture("installation_repositories_added")
    payload["repositories_added"][0]["full_name"] = full_name

    event = parse_installation_repositories_event(
        event_name="installation_repositories", payload=payload
    )

    assert event.added_repositories[0].full_name == full_name


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
