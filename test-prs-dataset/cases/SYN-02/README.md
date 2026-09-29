# SYN-02 oracle

`RequiredChecks` shows the three required CI checks in a list. The pre-image
maps a small fixed name list to one JSX item template. The patch manually
repeats the same `<li>` and nested `<span>` markup three times. It displays the
same names and classes in the same order, but every markup change now needs to
be made in three places. One `readability` / `low` finding is anchored on the
third repeated item at added line 10, yielding `attention`. The case source is
synthetic; neither the pre-image nor patch has an answer-hint comment.

Run the dependency-free oracle from the repository root with Node 22:

```sh
node test-prs-dataset/cases/SYN-02/oracle.mjs
```

The oracle checks the list values and item markup in both source forms after
applying the patch. It inspects the constrained TSX fixture directly; the API
repository has no React/TSX runtime, so this is a source-level equivalence
check rather than a browser rendering test.
