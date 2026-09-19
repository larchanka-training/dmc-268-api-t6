# Pytest unit-test conventions

Requires: api #4 (layout, SQLAlchemy)

## When to use

Any pure, dependency-free unit under test. Reference for this repo's conventions:
arrange/act/assert, a literal parametrize table, `pytest.raises(match=...)`, a fixture
returning a fake port, `monkeypatch` for environment variables.

## File placement

- `app/modules/<module>/domain/money.py`, `tests/test_money.py`

`default_currency()` below reads an env var for the proof only; in production such env
readers belong in `app/bootstrap/config.py`, not `domain/` (`domain/` stays stdlib / small
abstractions).

## Code

<!-- proof: app/money.py -->

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


class InsufficientFundsError(Exception):
    pass


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(f"amount must be non-negative, got {self.amount}")

    def add(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} != {other.currency}")
        return Money(self.amount + other.amount, self.currency)

    def subtract(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} != {other.currency}")
        if other.amount > self.amount:
            raise InsufficientFundsError(f"cannot subtract {other.amount} from {self.amount}")
        return Money(self.amount - other.amount, self.currency)


def default_currency() -> str:
    return os.environ.get("MONEY_DEFAULT_CURRENCY", "USD")
```

## Test

<!-- proof: tests/test_proof_pytest_unit.py -->

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import pytest

from app.__proof__.pytest_unit.money import InsufficientFundsError, Money, default_currency


class CurrencyPort(Protocol):
    def default(self) -> str: ...


@dataclass
class FakeCurrencyPort:
    value: str = "EUR"

    def default(self) -> str:
        return self.value


@pytest.fixture
def currency_port() -> CurrencyPort:
    return FakeCurrencyPort()


@pytest.mark.parametrize(
    ("amount", "other", "expected"),
    [
        (Decimal("10"), Decimal("5"), Decimal("15")),
        (Decimal("0"), Decimal("0"), Decimal("0")),
        (Decimal("2.50"), Decimal("2.50"), Decimal("5.00")),
    ],
)
def test_add_sums_same_currency(amount: Decimal, other: Decimal, expected: Decimal) -> None:
    # arrange
    left = Money(amount, "USD")
    right = Money(other, "USD")

    # act
    result = left.add(right)

    # assert
    assert result == Money(expected, "USD")


def test_subtract_raises_on_insufficient_funds() -> None:
    left = Money(Decimal("5"), "USD")
    right = Money(Decimal("10"), "USD")

    with pytest.raises(InsufficientFundsError, match="cannot subtract 10 from 5"):
        left.subtract(right)


def test_currency_port_fixture_returns_fake_value(currency_port: CurrencyPort) -> None:
    assert currency_port.default() == "EUR"


def test_default_currency_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONEY_DEFAULT_CURRENCY", "EUR")
    assert default_currency() == "EUR"

    monkeypatch.delenv("MONEY_DEFAULT_CURRENCY")
    assert default_currency() == "USD"
```

## Checklist

- Arrange/act/assert as three commented steps for the simplest case; skip the comments
  once a test is a one-liner. `parametrize` takes a literal tuple table, never one
  computed from the function under test.
- `pytest.raises(SomeError, match="...")` — `match` is a regex (applied with `re.search`);
  `re.escape()` the fragment if it contains regex metacharacters (`.`, `(`, `$`, …).
- A fixture returning a fake `Protocol` port is typed by the port, not the concrete fake
  class. `monkeypatch.setenv`/`delenv` for env vars — never mutate `os.environ` directly.
