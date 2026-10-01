# SYN-01 oracle

`collect_required_checks` produces the sorted, deduplicated CI check names for
a review summary. `sorted` already returns a list; the patch wraps that result
in another `list` call. It adds a redundant expression and an extra copy
without changing the function's observable output. The expected finding is
`readability` / `low` on the added return expression at line 7, yielding
`attention`. The case source is synthetic. Neither the pre-image nor the patch
contains an answer-hint comment.

Run the dependency-free oracle from the repository root:

```sh
uv run python test-prs-dataset/cases/SYN-01/oracle.py
```

It calls the public function with empty, repeated and generator inputs before
and after applying the patch to a temporary copy. Both versions return the
same sorted lists.
