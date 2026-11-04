# Graph-RAG v1 evaluation corpus

Inputs for the graph-RAG contribution eval of [issue #146](https://github.com/larchanka-training/dmc-268-api-t6/issues/146): 24 synthetic cases that compare a review without retrieved code (arm A) against one with the retrieved reverse-edge blocks (arm B). The method is [TEST_PLAN §3.5.1](../../docs/TEST_PLAN.md#351-запись-replay-и-гейт-146); the decision and the corpus shape are [ADR 0001 §12](../../docs/adr/0001-graph-rag-v1.md). The corpus shares [the case schema](../schema/case.schema.json) with the main benchmark but is validated by its own profile; the main `--final` rejects its cases.

## Case layout

```text
cases/RAG-01/
  case.json     # class "rag"; truths in expected_findings; the "rag" block (kind, defect_form,
                # expected_retrieved, absent_from_context, l4_definitions, injection)
  README.md     # oracle: story, why only the caller shows the defect, truth, review notes
  diff.patch    # unified patch against base/
  base/         # the pre-image, a small mini-repository
  index.json    # frozen graph-index snapshot of base/, generated, never edited by hand
```

`expected_retrieved` is the caller set arm B must retrieve (reverse_edge, injection), the hop-1 caller for a depth2 case, and, for a control or a clean case, the exact set it retrieves. The per-case integrity checks (`review/scripts/eval_rag_check.py`) run inside the validator and compare these declarations with the context the production build gives both arms. For a depth2 case they also require the hop-2 file to keep a `uses_imported` edge in the frozen `index.json` to a hop-1 symbol whose declaration arm B retrieves.

## Cases

| Case | Kind | Language | Defect form | Truth | Severity |
| --- | --- | --- | --- | --- | --- |
| [RAG-01](cases/RAG-01/README.md) | reverse_edge | python | new_required_param | `ledger/recording.py:7` | critical |
| [RAG-02](cases/RAG-02/README.md) | reverse_edge | python | narrowed_type | `stockroom/skus.py:9` | high |
| [RAG-03](cases/RAG-03/README.md) | reverse_edge | python | changed_return_shape | `clinic/schedule.py:20` | high |
| [RAG-04](cases/RAG-04/README.md) | reverse_edge | python | tightened_invariant | `uploads/retention.py:11` | high |
| [RAG-05](cases/RAG-05/README.md) | reverse_edge | python | renamed_export | `weather/forecast.py:7` | high |
| [RAG-06](cases/RAG-06/README.md) | reverse_edge | typescript | new_required_param | `src/shipping.ts:4` | high |
| [RAG-07](cases/RAG-07/README.md) | reverse_edge | typescript | changed_return_shape | `src/infusion.ts:6` | critical |
| [RAG-08](cases/RAG-08/README.md) | reverse_edge | typescript | tightened_invariant | `src/calendar.ts:6` | high |
| [RAG-09](cases/RAG-09/README.md) | reverse_edge | typescript | removed_export | `src/slug.ts:4` | high |
| [RAG-10](cases/RAG-10/README.md) | reverse_edge | typescript | renamed_export | `src/sensors.ts:4` | high |
| [RAG-11](cases/RAG-11/README.md) | control | python | - | `erasure/requests.py:15` | critical |
| [RAG-12](cases/RAG-12/README.md) | control | python | - | `promos/sales.py:15` | high |
| [RAG-13](cases/RAG-13/README.md) | control | python | - | `pulse/labels.py:15` | medium |
| [RAG-14](cases/RAG-14/README.md) | control | python | - | `fxdesk/quotes.py:16` | high |
| [RAG-15](cases/RAG-15/README.md) | control | typescript | - | `src/deposits.ts:12` | high |
| [RAG-16](cases/RAG-16/README.md) | control | typescript | - | `src/listing.ts:15` | high |
| [RAG-17](cases/RAG-17/README.md) | depth2 | python | changed_return_shape | `taskrunner/backoff.py:10` | high |
| [RAG-18](cases/RAG-18/README.md) | depth2 | python | tightened_invariant | `contacts/emails.py:7` | high |
| [RAG-19](cases/RAG-19/README.md) | depth2 | python | changed_return_shape | `statements/ordering.py:10` | high |
| [RAG-20](cases/RAG-20/README.md) | depth2 | typescript | changed_return_shape | `src/dates.ts:9` | high |
| [RAG-21](cases/RAG-21/README.md) | injection | python | changed_return_shape | `fleetlog/service.py:10` | high |
| [RAG-22](cases/RAG-22/README.md) | injection | python | tightened_invariant | `dispatch/windows.py:11` | high |
| [RAG-23](cases/RAG-23/README.md) | clean | python | - | none | none |
| [RAG-24](cases/RAG-24/README.md) | clean | typescript | - | none | none |

Distribution: 10 reverse_edge, 6 control, 4 depth2, 2 injection, 2 clean; 15 Python and 9 TypeScript; truths: 3 critical, 18 high, 1 medium; the renamed or removed exports are RAG-05, RAG-09 and RAG-10 (anchored on a RIGHT line). The table is generated from the `case.json` files.

## Commands

```sh
uv run python review/scripts/validate_dataset.py --root test-prs-dataset/rag --final --profile rag
uv run python review/scripts/build_rag_snapshot.py --check test-prs-dataset/rag/cases
uv run python review/scripts/build_rag_snapshot.py --write test-prs-dataset/rag/cases
uv run python review/scripts/eval_rag_rehearsal.py --root test-prs-dataset/rag
```

`--write` regenerates every `index.json` (after any change of a `base/` tree; the revisions derive from the case directory name, so a case is never renamed); `--check` fails on a missing or stale one. The rehearsal records and replays the chain with a fake model, spends nothing, and prints the projected input tokens per arm and the median B/A ratio the gate holds to 1.30. It takes about 3 minutes; `--out DIR` keeps the captures, `--require-gate` exits 3 when the gate is skipped, `--help` lists the rest.

## Manual review

Several truths can be matched in arm A by a generic comment inside the +-2 line window, with no knowledge of the caller. The report's manual root-cause column therefore requires a finding to name the caller; the per-case notes in each case README say which name or file to look for.

## Licence and attribution

All content is synthetic, authored for api#146; no third-party code. Each `case.json` carries the attribution.

## Captures

`responses/` holds the six recorded `eval-live` runs of the gate (`corpus=rag`, `policy_arm=none`, `review_prompt=v5`, `export_raw_responses=true`, model `mistral-small-4`, all `baseline_publishable`): the raw first answers of the 24 cases plus the `manifest.json` that `eval_replay.py` reads, copied unchanged from the `eval-live-raw-rag-…` artifacts.

| Directory | Arm (`rag_arm`) | Run id |
| --- | --- | --- |
| `A-run1`, `A-run2`, `A-run3` | A, no retrieved code (`none`) | 41720614606, 41724358586, 41725329228 |
| `B-run1`, `B-run2`, `B-run3` | B, reverse-edge blocks (`graph`) | 41720730389, 41724449009, 41725470313 |

```sh
for d in test-prs-dataset/rag/responses/*/; do
  uv run python review/scripts/eval_replay.py --root test-prs-dataset/rag --responses-dir "$d"
done
```

The recorded result and the computed gate verdict are in [the report](../../docs/test-plan/RAG_V1_CONTRIBUTION_REPORT.md); `reports/gate.json` is the gate script's output it quotes.

`oracle/hallucination.json` is the hallucination oracle the gate takes with `--oracle`: 11 findings the agent proposed and the tech lead confirmed on 2026-11-04; the six disputed RAG-01 #0 rows are not counted. The decisions and their reasons are in [the report](../../docs/test-plan/RAG_V1_CONTRIBUTION_REPORT.md#оракул-галлюцинаций).

A replay needs no network and no key; it rebuilds every case from its frozen `index.json` and takes about 40 seconds. The CI step "RAG sub-corpus" runs it for all six and fails on a non-zero exit; `tests/test_rag_captures.py` fails when a capture is missing, mislabelled or from a different model, prompt or corpus.
