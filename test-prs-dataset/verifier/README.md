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
`VFY-02`: their oracle stays unchanged and the declared untrusted field changes.
For title/body/suggestion attacks (`VFY-07`/`VFY-08`/`VFY-09`), the derived
production `candidate_id` also changes. The six marker attacks cover code,
title, body, suggestion, `.review/rules.md`, and `AGENTS.md`: six of the twelve
location × goal combinations, with both false-acceptance and false-rejection
goals represented. They do not establish general prompt-injection resistance.

The corpus is deliberately small; its rates describe these fixed cases, not a
population estimate. Run
`uv run pytest tests/test_verifier_eval_corpus.py` to check structural fields,
class/status/ref consistency, shown-code coordinates, and attack pairing before
evaluation. The validator cannot establish that the SQL or helper claims are
true, a policy applies, or an attack has the intended semantic effect. Those
labels and exact references require independent human review before a paid run.

This `verifier/v1` corpus is separate from the #163 `.review/rules.md` corpus
(`test-prs-dataset/rules/`, `RUL-01`–`RUL-08`). The latter measures reviewer
policy-arm behavior; this one measures decisions at the verifier boundary.
Both predeclare inputs and expected outcomes, validate corpus structure, and
keep live results separate from the case definitions.
