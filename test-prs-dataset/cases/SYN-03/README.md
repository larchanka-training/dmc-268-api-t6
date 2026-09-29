# SYN-03 oracle

`can_retry_review` decides whether a failed or cancelled review can be retried
within its limit. The pre-image expresses the two conditions directly. The
patch replaces the membership check with a `match` statement and a default
branch. It adds control-flow scaffolding for a two-state predicate without
changing any retry decision. One `readability` / `low` finding is anchored on
the added case arm at line 3, yielding `attention`. This is a synthetic case
with no answer-hint comments in the pre-image or patch.

Run the dependency-free oracle from the repository root:

```sh
uv run python test-prs-dataset/cases/SYN-03/syn_03_oracle.py
```

It executes the actual pre-image and patched function over 90 combinations of
status, retries used and retry limit, including empty status and boundaries,
and verifies the same decisions.
