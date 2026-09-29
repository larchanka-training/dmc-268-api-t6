# SYN-05 oracle

`render_run_label` formats a run identifier and state for a review summary. The
pre-image interpolates both values directly. The patch builds an intermediate
field dictionary and passes it into `format_map` for the same result. That adds
needless complexity to a fixed, two-field label without changing the behavior
for typed inputs. One `readability` / `low` finding is anchored on the added
`format_map` expression at line 6, yielding `attention`. The case is synthetic; neither
the pre-image nor patch has an answer-hint comment.

Run the dependency-free oracle from the repository root:

```sh
uv run python test-prs-dataset/cases/SYN-05/syn_05_oracle.py
```

It executes the pre-image and patched public function for 35 ID/state pairs,
including empty and Unicode states, negative and large IDs, text containing
template markers, and `int`/`str` subclasses with custom `__format__` methods.
Both versions return identical labels.
