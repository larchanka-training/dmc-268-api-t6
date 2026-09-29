# Gold benchmark case format

This directory holds the inputs and manually checked ground truth for [issue #30](https://github.com/larchanka-training/dmc-268-api-t6/issues/30). The [case schema](schema/case.schema.json) defines the metadata. Each case lives in `cases/<id>/`:

```text
cases/SEC-01/
  case.json
  diff.patch
  base/
    app/example.py
```

`base/` contains the pre-image with repository-relative paths. `diff.patch` is a unified Git patch against that pre-image. Commit both byte-for-byte. The validator copies `base/` to a temporary directory, runs `git apply --check`, and requires every ground-truth anchor to be an added line on the new side. A range needs both `start_line` and `line` on added lines. Duplicate ground-truth anchors within a case are invalid; final critical counts use distinct anchors. Do not put answer hints in patch comments. Pre-commit excludes dataset `.patch` files from hooks that would change their bytes.

For real upstream Python source that conflicts with this repository's lint or type settings, put the unchanged upstream tree under `base/.upstream/` and set `"apply_directory": ".upstream"` in `case.json`. The validator checks that this directory stays inside `base/` and uses `git apply --directory=.upstream --check`; patch and ground-truth paths remain the original upstream paths. A case-local `.ignore` may exclude only the copied upstream file from Ruff. [SEC-04](cases/SEC-04/README.md) records the exact source revisions and license for this layout.

Minimal synthetic `case.json` shape (the paths and lines below are illustrative):

```json
{
  "id": "SEC-01",
  "class": "security",
  "language": "python",
  "source": { "kind": "synthetic", "attribution": "QA team" },
  "base_path": "base",
  "patch_path": "diff.patch",
  "expected_verdict": "blocking",
  "expected_findings": [
    {
      "path": "app/example.py",
      "start_line": null,
      "line": 2,
      "severity": "critical",
      "category": "security"
    }
  ]
}
```

Use `class`: `security`, `resource`, `logic`, `syntax`, or `clean`; `language`: `python`, `typescript`, or `tsx`. A clean case has no expected findings. The class-to-category default is security → `security`, resource → `performance`, logic → `correctness`, syntax → `readability`; clean has no category. A resource bug whose concrete effect is correctness can use `correctness`; put the exception rationale in `category_reason` in `case.json` and explain it in the case notes. `severity` and `category` values match the [ReviewOutput contract](../review/schemas/review-output.schema.json), but case metadata uses its own schema. `expected_verdict` is derived from ground-truth severity: critical/high → `blocking`, medium/low → `attention`, otherwise `clean` ([PIPELINE_SPEC §11](../docs/PIPELINE_SPEC.md#11-вердикт-и-бейджи-d3)).

For `source.kind: "real"`, include `url` (stable PR/commit URL), `revision`, `license_name`, `license_url`, and `attribution`. A curator must inspect the source revision, confirm the license permits the exact excerpt, and manually verify the ground truth. Synthetic cases still require attribution and must use unmistakably fake credentials, for example `FAKE_TEST_CREDENTIAL_DO_NOT_USE`; never copy a working secret. The case validator checks metadata presence and patch/anchor mechanics. It cannot establish legal permission or semantic truth.

## Validation

```sh
uv run python review/scripts/validate_dataset.py --case SEC-01
uv run python review/scripts/validate_dataset.py
uv run python review/scripts/validate_dataset.py --final
uv run python review/scripts/validate_dataset.py --final --report-json /tmp/issue-30-dataset-report.json
```

The first command checks one case, the second all present cases. `--final` also checks distribution: 20–30 cases, at least four per class, at least five licensed real cases with one per class, Python and TypeScript/React, and at least five critical truths. `--report-json` requires `--final` and writes deterministic counts for all and valid cases, classes, real sources, languages and distinct critical anchors, plus `passed` and diagnostics; invalid cases never contribute truth counts. Invalid schema, malformed/unapplicable patches, missing pre-images, non-added anchors, and wrong verdicts return nonzero with the case ID and reason. Run `git apply --check` again after pre-commit when adding cases.

## Curated corpus

The 24 active inputs pass final schema, patch-application and distribution validation. The class counts are security **4**, resource **5**, logic **5**, syntax **5**, and clean **5**. Five cases use distinct licensed real PRs, one in each class. Five separate critical truth anchors come from synthetic cases. The omitted SEC-05 slot has no case or response entry and is excluded from future replay/live denominators. No model response or quality baseline has been recorded yet.

### Real PR provenance and checked truth

Each case's linked notes record the exact base/head revisions, retained license text, patch scope and ground-truth oracle. Independent case reviews checked source and license identity and inspected the stated behavior; the validator checks mechanics but cannot establish semantics or legal permission by itself.

| Case | Source and attribution | License at base revision | Checked result |
| --- | --- | --- | --- |
| [SEC-04](cases/SEC-04/README.md) | [Flask PR #5632](https://github.com/pallets/flask/pull/5632), davidism | [BSD-3-Clause](https://github.com/pallets/flask/blob/7522c4bcdb10449dc919e0ffbdebb92fe66822b5/LICENSE.txt) | Fallback signing key order confirmed by the Flask advisory; low security finding. |
| [RES-03](cases/RES-03/README.md) | [aiohttp PR #7944](https://github.com/aio-libs/aiohttp/pull/7944), xiangxli | [Apache-2.0](https://github.com/aio-libs/aiohttp/blob/2670e7b08da179e74a643dca8d795fd23fcd282e/LICENSE.txt) | Defaultdict lookup grows CookieJar buckets; upstream issue/fix and executable oracle corroborate high performance impact. |
| [LOG-03](cases/LOG-03/README.md) | [python-snap7 PR #806](https://github.com/gijzelaerr/python-snap7/pull/806), gijzelaerr | [MIT](https://github.com/gijzelaerr/python-snap7/blob/d72b66fb4a7e64c7c11ea9caa0ad64d4345d34c9/LICENSE) | Extra COTP byte conflicts with declared length; frame oracle and upstream client report corroborate high correctness impact. |
| [SYN-04](cases/SYN-04/README.md) | [Zustand PR #725](https://github.com/pmndrs/zustand/pull/725), devanshj | [MIT](https://github.com/pmndrs/zustand/blob/76eeacb1c448ea323f46c2956ffb66c307c746b5/LICENSE) | Duplicate type augmentation is identical; upstream fix and source oracle corroborate low readability impact. |
| [CLEAN-03](cases/CLEAN-03/README.md) | [Zustand PR #663](https://github.com/pmndrs/zustand/pull/663), dai-shi | [MIT](https://github.com/pmndrs/zustand/blob/e3566f9b54c520343b9318720ac3dd9b8397bed7/LICENSE) | React hook body and public re-exports remain equivalent after the extraction; no expected finding. |

### Critical truth rationale

Each row points to one distinct new-side anchor. Severity is tied to the concrete fixture impact, not to the target count.

| Case | Why `critical` is supported |
| --- | --- |
| [SEC-01](cases/SEC-01/README.md) | A published constant passes the private-record authorization check. |
| [SEC-02](cases/SEC-02/README.md) | Injected account input returns another account's orders, exposing cross-tenant data. |
| [RES-05](cases/RES-05/README.md) | Unbounded retained batches hit the worker's memory ceiling and stop settlement processing until replacement. |
| [LOG-02](cases/LOG-02/README.md) | Pending payments release shipments before capture, causing systemic financial exposure. |
| [LOG-05](cases/LOG-05/README.md) | A declined publisher receipt removes ready jobs from the only pending store, causing durable job loss. |

Case metadata deliberately contains **no model response or model/prompt provenance**. It is valid before #33 selects a model and before any baseline exists. Replay/live tasks will keep raw responses and a separate manifest with case ID, model ID, prompt path/version/SHA, run metadata, and response path. Missing or invalid recorded responses must remain visible to eval rather than being rewritten into valid answers. Quality metrics and manual semantic adjudication follow [TEST_PLAN §3](../docs/TEST_PLAN.md#3-методология-llm-eval).
