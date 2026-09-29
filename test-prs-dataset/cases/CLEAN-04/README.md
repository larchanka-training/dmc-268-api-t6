# CLEAN-04 oracle

`formatCheckSummary` formats the counts shown in a review run summary. The
patch marks its input count fields `readonly`, expressing that the formatter
observes an immutable snapshot. It changes only TypeScript type declarations;
the function body and emitted JavaScript behavior are unchanged. The expected
finding list is empty and the verdict is `clean`. This is a synthetic case,
with no benchmark answer hints in its pre-image or patch.

Run the dependency-free oracle from the repository root with Node 22:

```sh
node test-prs-dataset/cases/CLEAN-04/oracle.mjs
```

It uses Node's TypeScript type stripping to import the actual pre-image and
patched module, compares the emitted function text, then checks seven outputs
with frozen inputs, including zero counts, skipped checks, negative and
fractional values, and a large count.
