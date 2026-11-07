# Tests

## Test setup

Every test under `src/features/*/__tests__/` builds its own data, so that it
reads top to bottom without a factory. Duplicated setup in those test files is
intended: do not report it as duplication.

## Real time in tests

Tests never wait on real time. Report a real `setTimeout` in a test as a
correctness problem: the suite turns slow and flaky, and `vi.useFakeTimers()`
covers these cases.
