# Good and Bad Tests

## Good Tests

**Integration-style**: test through real interfaces, not mocks of internal
parts.

```python
# GOOD: tests observable behavior of a use case
from app.modules.reviews.application.submit_finding import SubmitFinding
from tests.fakes import InMemoryFindingRepository


def test_submit_finding_persists_a_new_finding() -> None:
    repo = InMemoryFindingRepository()
    use_case = SubmitFinding(repo)

    result = use_case.execute(run_id="r_1", line=42, body="unclear naming here")

    assert result.line == 42
    assert repo.get(result.id).body == "unclear naming here"
```

Characteristics: tests behavior callers care about; uses the public API only
(use-case `execute`, repository methods, HTTP response via `TestClient`);
survives internal refactors; describes WHAT, not HOW; one logical assertion
per test.

## Bad Tests

Red flags: mocking internal collaborators; testing private helpers;
asserting on call counts/order instead of outcomes; test breaks when
refactoring without a behavior change; reading a repository's internal
session state instead of going through its public methods.

```python
# BAD: reaches into the repository's internal dict
def test_add_finding_stores_a_new_finding() -> None:
    repo.add(finding)
    assert len(repo._rows) == 1  # noqa: internal field


# GOOD: verifies through the exported interface
def test_add_finding_makes_it_retrievable() -> None:
    repo.add(finding)
    assert repo.get(finding.id) == finding
```

**Tautological**: expected value restates the implementation, so the test
passes by construction.

```python
# BAD: expected value is recomputed the way the code computes it
def test_sums_finding_counts() -> None:
    findings = [{"count": 3}, {"count": 2}]
    assert sum_findings(findings) == sum(f["count"] for f in findings)


# GOOD: expected value is an independent, known literal
def test_sums_finding_counts() -> None:
    assert sum_findings([{"count": 3}, {"count": 2}]) == 5
```

## Parametrize with literal tables, and `pytest.raises`

```python
import pytest


@pytest.mark.parametrize(
    ("status", "expected_terminal"),
    [("queued", False), ("completed", True), ("cancelled", True)],
)
def test_run_status_terminal(status: str, expected_terminal: bool) -> None:
    assert is_terminal(status) is expected_terminal


def test_submit_finding_rejects_a_line_outside_the_diff() -> None:
    use_case = SubmitFinding(InMemoryFindingRepository())
    with pytest.raises(ValueError, match="line outside diff"):
        use_case.execute(run_id="r_1", line=9999, body="x")
```

## Integration tests: skip without `TEST_DATABASE_URL`

```python
import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="no TEST_DATABASE_URL")
def test_repository_persists_a_finding_row() -> None: ...
```
