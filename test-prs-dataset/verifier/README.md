# Fixed verifier corpus (v1)

`v1/cases.json` is a synthetic input set with provisional labels for issue #217. It is
separate from the 24-case reviewer baseline: each case starts at the verifier
boundary with one candidate (or none), the exact code lines shown to the
verifier, and the two policy documents. The `expected` object records the
proposed decision and its code/policy coordinates under #107. The labels did
not come from `StubVerifierModel` or the live model. Independent human validation
must be recorded before the paid run.

`VFY-01` proves a SQL interpolation finding. `VFY-02` shows the same claim
against a bound-parameter query. `VFY-03` applies a Go naming rule to Python.
`VFY-04` claims behavior of a helper whose implementation is absent from the
shown context. `VFY-05` has no candidate and therefore must not call the
verifier. The six remaining cases pair an injected input with `VFY-01` or
`VFY-02`: their oracle stays unchanged and only the declared untrusted field
differs. They cover code, title, body, suggestion, `.review/rules.md`, and
`AGENTS.md`, with both false-acceptance and false-rejection goals.

The corpus is deliberately small; its rates describe these fixed cases, not a
population estimate. Run
`uv run pytest tests/test_verifier_eval_corpus.py` to check required fields,
class coverage, and attack pairing before evaluation.
