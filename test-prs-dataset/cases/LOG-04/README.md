# LOG-04 oracle

A review-run React hook uses a reducer to track the current run. Cancellation is
terminal for that run: a completion event delivered after cancellation must not
turn it into a passed review. The pre-image enforces that transition. The patch
broadens the completion guard to accept a cancelled run, so a late event paints
the cancelled review as passed. Normal running-to-passed behavior and events for
a different run remain unchanged.

The ground truth is one `correctness` / `medium` finding on the changed guard at
line 32, yielding `attention`. This is a misleading UI state and can affect a
reviewer's decision, but the fixture does not establish that a server-side
merge or publish gate depends on the hook, so a critical claim would be
unsupported.

Run the dependency-free oracle from the repository root with Node 22:

```sh
node test-prs-dataset/cases/LOG-04/oracle.mjs
```

It executes the actual pre-image and patched reducer through Node's built-in
TypeScript stripper and checks cancellation, late completion, normal completion,
and unrelated-run behavior. The source is synthetic and contains no answer-hint
comments.
