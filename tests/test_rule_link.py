"""The link from a GitHub comment to the ``.review/rules.md`` lines of its rule (#465)."""

from __future__ import annotations

from app.modules.reviews.application.verdict import HashedFinding, findings_hash

_LINK = (
    "https://github.com/acme/widgets/blob/"
    "1111111111111111111111111111111111111111/.review/rules.md?plain=1#L5-L8"
)


def _hashed(rule_link: str | None = None) -> HashedFinding:
    return HashedFinding(
        path="a.py",
        line_start=3,
        line_end=None,
        severity="high",
        category="correctness",
        title="T",
        body="B",
        suggestion=None,
        inline=True,
        rule_link=rule_link,
    )


def test_a_finding_without_a_link_keeps_its_hash() -> None:
    # Taken from main before the field existed: the pointer of a run already in publishing
    # at the deploy must still match its rows.
    assert findings_hash([_hashed()]) == (
        "190d59b987ba190b4c643c18d98a69f0f12c73f5ee0420ee87a317c807d4ebb1"
    )


def test_a_new_link_makes_a_new_hash() -> None:
    # The marker is looked up across the whole pull request: with the same hash, a run whose
    # comment text differs would take an earlier review for its own.
    other = _LINK.replace("#L5-L8", "#L5-L9")

    assert (
        len(
            {
                findings_hash([_hashed()]),
                findings_hash([_hashed(_LINK)]),
                findings_hash([_hashed(other)]),
            }
        )
        == 3
    )
