# CLEAN-05 oracle

The fixture represents webhook header handling: `canonicalize_headers` lowers
names and trims values, while `delivery_metadata` reads the delivery and event
headers from that normalized mapping. The patch replaces a manual accumulation
loop with an equivalent dictionary comprehension. Both forms preserve the
mapping's iteration order and last value for names that become equal after
lowercasing. The change has no expected findings and verdict `clean`.
The source is synthetic, with no answer-hint comments in the pre-image or patch.

Run the dependency-free oracle from the repository root:

```sh
uv run python test-prs-dataset/cases/CLEAN-05/clean_05_oracle.py
```

It executes both versions of the actual functions for eight mappings,
including empty input, mixed-case names, case-folding collisions in both
orders, Unicode text, a whitespace-bearing name, a read-only mapping and an
ordered mapping. It compares normalized item order and downstream metadata.
