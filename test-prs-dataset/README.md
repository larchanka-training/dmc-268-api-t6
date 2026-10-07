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

The 24 active inputs pass final schema, patch-application and distribution validation. [PR #68](https://github.com/larchanka-training/dmc-268-api-t6/pull/68) merged the [#66](https://github.com/larchanka-training/dmc-268-api-t6/issues/66) normalization work, including PIPELINE_SPEC §9, before the first baseline capture. Issue #66 is CLOSED; the merged normalization is no longer a pending baseline dependency. The class counts are security **4**, resource **5**, logic **5**, syntax **5**, and clean **5**. Five cases use distinct licensed real PRs, one in each class. Five separate critical truth anchors come from synthetic cases. SEC-05 was omitted after two candidate fixtures were stopped by the automatic safety filter; four security cases still meet the minimum. It has no case or response entry and is excluded from future replay/live denominators. The current recorded baseline is [Mistral via EUrouter](#recorded-mistral-baseline--2026-10-07); the earlier [Nemotron via OpenRouter measurement](#historical-nemotron-baseline--2026-10-06) is retained as historical context.

### Issue #53 corpus decision (2026-10-04)

Keep **24 active cases** for the first live baseline. No case was added or promoted: the validator proves patch and anchor mechanics, but it cannot supply an independent semantic or license review. The baseline still requires one raw first answer per active case, so its denominator remains 24.

| Requested gap | Decision and reason |
| --- | --- |
| Multiple truths in one case | **Defer.** None of the 24 active cases has more than one expected finding. A new multi-truth patch needs two independently checked defects and distinct added-line anchors in the same coherent change; combining existing truths just to exercise the scorer would make the benchmark claim unsupported. Scorer unit tests cover duplicate/multiple-anchor mechanics. |
| Range truth | **Defer.** Every active finding has `start_line: null`. A range case needs a reviewed defect spanning two added new-side anchors, with both endpoints and the semantic range checked; `git apply --check` and anchor validation alone cannot establish that truth. Scorer unit tests cover range mechanics. |
| Licensed real high/critical security truth | **Defer.** The only real security case, SEC-04, is supported as **low** by its recorded Flask source and advisory; promoting its severity would contradict that evidence. No additional high/critical security PR has a curated source revision, license permission for the exact excerpt, and independently checked impact. The synthetic critical truths remain explicitly synthetic. |
| SEC-05 | **Keep omitted.** Two earlier candidate fixtures were stopped by the automatic safety filter, and no independently reviewed safe replacement exists. Four security cases satisfy the final distribution rule; SEC-05 has no active input or response and is excluded from the 24-case denominator. |

Any later addition must receive its own source/license and ground-truth review, pass `validate_dataset.py --final`, and change issue #53's literal 24-response criterion **before** live capture. A case or truth edit after capture requires a complete fresh baseline.

### Real PR provenance and checked truth

Each case's linked notes record the exact base/head revisions, retained license text, patch scope and ground-truth oracle. Independent case reviews checked source and license identity and inspected the stated behavior; the validator checks mechanics but cannot establish semantics or legal permission by itself.

Case-local `oracle.mjs` and `*_oracle.py` files support human fixture verification. They are never sent to the model and are not inputs to the replay harness; replay reads only case metadata, patches, and recorded raw responses.

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

The recorder scores the **first call**, even if a retry, repair, or fallback later succeeds. A first-call HTTP 429 or timeout therefore records an empty response and counts as invalid; the later valid answer never replaces it. The manifest distinguishes `first_call: no_content` (a provider call failed before an answer), `empty_answer` (a provider answer had no text), and `no_call` (the gateway did not send a request). A successful later fallback can make a transient first-call failure part of a publishable, measured raw-first baseline, while its invalid response remains in the denominator.

Terminal provider or infrastructure failure for a case, such as HTTP 402 on both primary and fallback or invalid accounting metadata on a paid HTTP 200 response, sets `run_metadata.baseline_publishable` to `false` and lists affected IDs in `nonpublishable_case_ids`. The safe per-case `paid_metadata_error` marker distinguishes the paid-metadata failure from ordinary invalid model text; the first raw answer remains recorded even when its JSON validates. The recorder saves all first raw responses, statuses, and the full manifest for diagnosis; both live and offline replay CLI return nonzero, and replay warns that the capture is not publishable. Capture is prepared in a same-filesystem temporary directory and promoted only after every response and the manifest are ready; an unexpected mid-corpus exception leaves no partial baseline, and an existing populated `responses/` is preserved. After inspecting the diagnostic capture and resolving the failure, manually delete or move `test-prs-dataset/responses/` out of the dataset before rerunning all 24 cases; the recorder refuses to overwrite an existing populated directory. Publish metrics or responses only from the complete replacement capture.

For each case, `run_metadata.cases` records the requested `first_model` and the actual first response's serving `provider` as `first_provider_label` plus a domain-separated `first_provider_digest`. Known EUrouter route names use safe public labels; any other provider string becomes `custom` with its digest, and a first call with no provider response records nulls. A paid but rejected first response keeps its available provider identity. This differs from the configured provider in `effective_settings`. Report observed provider labels and any custom digests with the measured baseline, without copying raw provider strings, URLs, or keys into the manifest or README.

The manifest also stores sorted `static_inputs`, `static_digest`, and `corpus_digest`. Each digest is `sha256-v1:<64 lowercase hex>` over a SHA-256 stream beginning with the bytes `review-eval-inputs-v1` followed by a NUL byte. For every unique relative POSIX path in sorted order, the stream then contains its UTF-8 path length as an unsigned 8-byte big-endian integer, the path bytes, its file-content length in the same format, and the exact file bytes. Static inputs are the selected review system prompt, all literal `review/rules/*.json` files (including the rule schema), selected custom rule files, and these code paths. Each remains in the byte-level digest because a change can affect the provider request, whether a response is accepted or retried, or which raw answer the recorder captures:

| Static code path | Why it is included |
| --- | --- |
| `app/bootstrap/llm_gateway.py` | Composes requests and sets attempt deadlines. |
| `app/common/application/languages.py` | Selects the case language and default rule set. |
| `app/modules/reviews/application/llm.py` | Defines call kinds and retryability used by the gateway. |
| `app/modules/reviews/application/prompt_budget.py` | Chooses whole, truncated, and omitted diff files. |
| `app/modules/reviews/application/prompt_builder.py` | Renders the system/user message envelope. |
| `app/modules/reviews/application/review_output.py` | Parses and validates review output. |
| `app/modules/reviews/application/run_failures.py` | Supplies the gateway's attempt deadline and retryable codes. |
| `app/modules/reviews/infrastructure/llm/answers.py` | Validates answers and prepares repair feedback. |
| `app/modules/reviews/infrastructure/llm/gateway.py` | Controls calls, retries, repair, fallback, and budgets. |
| `app/modules/reviews/infrastructure/llm/models.py` | Adapts gateway calls to review and conventions model ports. |
| `app/modules/reviews/infrastructure/llm/settings.py` | Resolves model profiles, endpoints, and call policy. |
| `app/modules/reviews/infrastructure/llm/transport.py` | Encodes provider requests and extracts response text. |
| `review/schemas/review-output.schema.json` | Defines strict response format and validation. |
| `review/scripts/eval_live.py` | Assembles each case request and selects first-call raw content. |
| `app/modules/reviews/application/conventions_prompt.py` | Renders conventions input when a conventions prompt is supplied. |

A supplied conventions prompt and its renderer are included only for that task. Byte hashing also warns on a docstring-only edit to any listed file; this conservative warning avoids missing a changed deadline or call policy. `run_failures.py` remains included because `FAST_ATTEMPT_DEADLINE` and `RETRYABLE_ERROR_CODES` are imported into the gateway path. Corpus inputs are every active `case.json`, patch, and recursive pre-image file. Every case ID maps exactly to `responses/<case-id>.json`; replay rejects unmapped extra files. Replay warns if either digest differs or a static file is missing; it never rewrites raw answers. Refresh the **entire** response set and manifest after any input edit, even if only one rule or case changes. Traversal and symlink inputs are rejected.

## Recorded Mistral baseline — 2026-10-07

The complete 24-case capture in [`responses/manifest.json`](responses/manifest.json)
uses `mistral-small-4` through EUrouter, with configured fallback
`mistral-small-3.2-24b`. It was recorded by
[Actions run 37607360909, attempt 1](https://github.com/larchanka-training/dmc-268-api-t6/actions/runs/37607360909)
on source commit `36c976b05f383d4f4af79d3cbce1c9e61dab093e`, starting at
`2026-10-07T10:26:17.148179Z` (12:26:17 Europe/Warsaw). This was one paid
workflow run with empty `model` and `export_raw_responses=true`.

The prompt is `review/prompts/review.system.v2.md` (`v2`), SHA-256
`9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75`.
The engine is `fast`; primary structured output is `json_schema`, context window
262144 tokens, output limit 8000 tokens, and per-call timeout 90 seconds.
The manifest retains full effective settings and input digests. All 24 first calls
used `mistral-small-4` and reported the serving-provider label `mistral`, digest
`sha256-v1:8e1af0131864f4488dc577f497afcf5c87b74b237e86f0322abf99e8c2ef018a`.
The configured provider label is `eurouter`. No arbitrary provider string is
published. Every gateway case eventually reported `accepted` with no terminal
provider/infrastructure failure; `baseline_publishable` is `true`.

All 24 responses and the manifest were copied byte-for-byte from the opt-in raw
artifact. Each response's SHA-256 and byte count match the separate safe metadata
artifact; the raw and safe manifest copies also match. At the publication commit
`ba99c11`, offline replay had no drift warning, and its entire report after
removing validator diagnostics matched the live safe report. Later edits to
fingerprinted gateway files make replay on `main` print a static-input drift
warning (now `Static input digest mismatch; refresh the complete baseline`). It
clears only when the complete baseline is re-captured, next planned in #82; see
[Baseline drift](#baseline-drift). These results score only the first raw answer;
publishability and eventual gateway acceptance do not imply a quality threshold.

| Metric | Baseline |
| --- | --- |
| First raw response validity | 20.8% (5/24) |
| Micro TP / FP / FN | 1 / 1 / 18 |
| Micro precision | 50.0% |
| Micro recall | 5.3% |
| Critical recall | 20.0% (1/5) |
| Verdict agreement | 8.3% (2/24) |
| Severity mismatches | 1 |

| Category | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| security | 0 | 0 | 4 | undefined | 0.0% |
| correctness | 1 | 1 | 4 | 50.0% | 20.0% |
| performance | 0 | 0 | 5 | undefined | 0.0% |
| readability | 0 | 0 | 5 | undefined | 0.0% |

The offline validator rejects 19 first answers. These disjoint groups account
for all 19: custom-rule attribution-prefix failures only (11); attribution plus
`start_line >= line` (4); attribution plus line-range and severity/confidence
ordering failures (2); line-range failures only (2). There are no missing/empty
first answers or JSON parse failures. All raw bytes remain unchanged, and all 24
cases remain in the denominator. The one severity mismatch is LOG-02, predicted
`high` versus ground-truth `critical`. These are mechanical scorer results, not
manually adjudicated semantic accuracy.

## Historical Nemotron baseline — 2026-10-06

The prior 24-case capture in the
[historical manifest](https://github.com/larchanka-training/dmc-268-api-t6/blob/cb365cb872f4ff6987b7abc631413017973c50d1/test-prs-dataset/responses/manifest.json) used
`nvidia/nemotron-3-super-120b-a12b:free` through OpenRouter with
`review/prompts/review.system.v2.md` (version `v2`). Recording started at
`2026-10-06T20:43:54.136922Z` (22:43:54 Europe/Warsaw). Capture source commit:
`995059a6df811d111d8a10d65884d4c12ceff754`.

This is a baseline for that free model and route, not evidence for the selected
EUrouter Mistral models or their two-model live conventions/D7 acceptance criterion.
No fallback model was configured. The engine was `fast`, structured output was
`json_schema`, the context window was 262144 tokens, and the output limit was
8192 tokens. The per-call timeout was 90 seconds. The full effective settings and
input digests are in the manifest.

Configured provider OpenRouter is recorded as `custom` with digest
`sha256-v1:5044b4f1c460e88a5fb9af9debfee5ecbbdd846289e93635ba781251ed4ba231`.
For the 23 first calls with provider responses, the observed serving-provider label
is `custom` with digest
`sha256-v1:6903649630881d2de78976de011006c49db83b4b727e184cce5ae1171124e7ba`;
SEC-04 has no first-response provider identity. The manifest deliberately retains
safe labels and digests rather than arbitrary provider strings; it does not
provide a human-readable serving-provider name for this capture.

| Metric | Baseline |
| --- | --- |
| First raw response validity | 66.7% (16/24) |
| Micro TP / FP / FN | 3 / 5 / 16 |
| Micro precision | 37.5% |
| Micro recall | 15.8% |
| Critical recall | 40.0% (2/5) |
| Verdict agreement | 29.2% (7/24) |
| Severity mismatches | 2 |

| Category | TP | FP | FN | Precision | Recall |
| --- | --- | --- | --- | --- | --- |
| security | 1 | 0 | 3 | 100.0% | 25.0% |
| correctness | 2 | 4 | 3 | 33.3% | 40.0% |
| performance | 0 | 1 | 5 | 0.0% | 0.0% |
| readability | 0 | 0 | 5 | undefined | 0.0% |

These historical mechanical scorer results are not a claim of manually adjudicated
semantic accuracy. The historical responses remain accessible at the linked
revision, including empty or invalid text.
CLEAN-03, LOG-02, RES-03, SEC-02 and SEC-03 have `empty_answer`; SEC-04 has
`no_content`. All six later reached `accepted`, but the missing first answers still
count as invalid. LOG-03 ended with `llm_invalid_output` and has truncated JSON.
SYN-04 fails the offline attribution-prefix check for its custom rule, accounting
for the eighth invalid first answer. The manifest marks the
capture publishable because no terminal infrastructure/provider failures remain;
that flag does not imply high quality or completion of all issue #53 criteria.

Replay the current committed Mistral capture without credentials or network calls:

```sh
uv run python review/scripts/eval_replay.py
```

The existing required `Python lint / type / test` job runs the same replay
unconditionally. A missing manifest fails the job and writes a missing-baseline
error to the job summary. Replay metrics are included only when a replay report
exists. Quality values are reported rather than used as pass thresholds. Raw response files are excluded
from whitespace and end-of-file rewriting hooks so a commit preserves exact bytes.

Corpus expansion is deferred for this baseline: keep the already curated 24-case
corpus fixed so this measurement can be reproduced. Multi-truth and range-truth
cases, a real high/critical security case, and reconsideration of SEC-05 remain
future curation work; expanding the corpus will require a separately recorded
baseline and updated denominators.

### Manual live evaluation in GitHub Actions

[Live corpus evaluation](../.github/workflows/eval-live.yml) is a manual
`workflow_dispatch` workflow, separate from required offline replay. It uses the
team EUrouter account: repository variables `LLM_MODEL` and `LLM_FALLBACK_MODEL`
select the primary and fallback models (currently `mistral-small-4` and
`mistral-small-3.2-24b`). Their endpoint, context window, output limit and pricing
come from the gateway's known-model profiles. The organization Actions secret
`AI_DMC268_T6` must be available to this repository; it is exposed only to the
live-evaluation step. No local copy of the key is needed.
After the workflow exists on the default branch, select **Actions → Live corpus
evaluation → Run workflow** and choose the branch to evaluate. Leave **model**
empty to use `vars.LLM_MODEL`, or set `mistral-small-4` and
`mistral-small-3.2-24b` in separate runs to evaluate each selected model as primary.
The input overrides only the primary model and does not change repository
variables used by staging. The fallback remains `vars.LLM_FALLBACK_MODEL`, so
selecting `mistral-small-3.2-24b` as primary can make both profiles use the same
model. If fallback is reached, it makes an additional call to that same model.
This does not change the raw-first metrics: they score the first primary response,
not repair or fallback responses.

The workflow copies only `cases/` and `schema/` to the runner's temporary directory,
validates the corpus, and records new responses there. It never replaces the
committed baseline. The summary uses the same formatter and metrics as offline
replay. Provider/infrastructure failures fail the job; available diagnostic metrics
are still summarized and uploaded. Missing credentials fail before model calls.

The 14-day artifact contains `eval-report.json` (without validator diagnostics) and
`eval-response-metadata/` (per-case hashes, byte counts and statuses, plus the
generated safe manifest with first model and sanitized provider identity).
The report and manifest provenance retain recording time,
fallback model and sanitized effective settings; secrets, raw endpoint URLs and
arbitrary provider metadata are excluded. It does not
contain raw model text, provider envelopes or credentials.

The boolean **export_raw_responses** input defaults to `false`. Leave it unchecked
for metrics-only runs. Explicitly enable it when preparing a committed baseline:
the separate `eval-live-raw-<run-id>-<attempt>` artifact retains only the generated
case-response files and `manifest.json` for 14 days. It preserves all answer bytes,
including empty or invalid first answers, and excludes unexpected files, logs,
prompts and provider envelopes. The export validates the manifest against the
evaluated case IDs and rejects missing files or symlinks before upload. Raw model
text becomes downloadable with this opt-in; review it before publication.
Verify `run_metadata.baseline_publishable`, completeness and each response's
SHA-256/byte count against the safe metadata artifact before copying the full
capture unchanged into `responses/`. The manifest records paths and provenance;
per-response hashes are in the safe metadata artifact. Replay the copied capture
offline and check for drift before committing it.

A run spends the team EUrouter balance, including
retries/repair, can take tens of minutes, and has a 60-minute job timeout. Concurrent
runs of this workflow are serialized. This workflow does not verify conventions
on the two selected EUrouter models.


### Baseline drift

The static digest includes gateway/settings inputs. The Mistral capture above
replayed without drift on its source commit and its publication change. Later
changes to fingerprinted inputs have already produced a drift warning on `main`
(see the recorded baseline above); preserve the original capture and its
provenance rather than editing its digest to hide drift.
Refresh the entire baseline in a separately authorized run when needed.
