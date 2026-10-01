# CLEAN-01 oracle

The pre-image calculates finding recall and precision as percentages in two
functions. Both use the same zero-denominator rule and arithmetic expression.
The patch extracts that shared calculation into a small helper. It keeps the
division and multiplication order, so fractional results are not rounded or
otherwise changed. This repairs the old TC-01 idea, in which rounding altered
the metric. There are no expected findings, and the expected verdict is
`clean`. The case is synthetic; the patch has no answer-hint comments.

Run the dependency-free oracle from the repository root:

```sh
uv run python test-prs-dataset/cases/CLEAN-01/clean_01_oracle.py
```

It compares the actual pre-image and patched functions over 198 combinations
of matched and total counts. The combinations include zero and negative totals,
fractions with repeating binary representations, over-100% values, and large
integers around `2**53`. Literal checks for `1/3` and `2/3` retain the
unrounded percentage values.
