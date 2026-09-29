# CLEAN-02 oracle

The pre-image includes the complete `SummaryLine` and `RunSummaryLine` classes.
`RunSummaryLine.render` explicitly passes its class and instance to `super`.
The patch changes that call to zero-argument `super()`. Inside this instance
method, both forms start lookup after `RunSummaryLine` in the runtime method
resolution order and pass the same arguments. The expected finding list is
empty and the verdict is `clean`.

Run the dependency-free oracle from the repository root:

```sh
uv run python test-prs-dataset/cases/CLEAN-02/clean_02_oracle.py
```

It executes both versions over 192 combinations: direct instances and a
multiple-inheritance class with an intervening render method, three run IDs,
four titles and eight score values. The values include `None`, signed zero,
fractional scores, infinity and NaN. Both versions return identical strings.
The case is synthetic, and neither the pre-image nor patch has answer-hint
comments. `.ignore` only excludes the intentional pre-image from this repo's
Ruff rule that would auto-fix it before the benchmark can observe the change.
