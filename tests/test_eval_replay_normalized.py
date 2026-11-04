"""The second replay column (#239): the first answers as the gateway accepts them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from review.scripts.eval_replay import ReplayError, format_console, replay

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = REPO_ROOT / "test-prs-dataset"


def attributed(rule: str) -> str:
    return f"According to custom instructions in '{rule}' (rule set): The name misleads."


def answer_finding(**overrides: Any) -> dict[str, Any]:
    """A ReviewOutput v1 finding on the critical truth of SEC-01."""
    return {
        "path": "app/authorization.py",
        "line": 3,
        "start_line": None,
        "severity": "critical",
        "category": "security",
        "title": "Hardcoded credential",
        "body": "The token is committed to the source.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
        **overrides,
    }


def answer(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "findings": list(findings),
        "summary": {"problem": "A credential leaks.", "done_well": "Short.", "effort": "small"},
    }


def one_case_capture(
    tmp_path: Path,
    body: object,
    *,
    case_id: str = "SEC-01",
    prompt_version: str = "v3",
    arm: str = "none",
) -> Path:
    """A dataset of one case; its manifest is the committed one narrowed to that case."""
    root = tmp_path / "dataset"
    shutil.copytree(DATASET_ROOT / "cases" / case_id, root / "cases" / case_id)
    responses = root / "responses"
    responses.mkdir()
    (responses / f"{case_id}.json").write_text(json.dumps(body), encoding="utf-8")
    manifest = json.loads((DATASET_ROOT / "responses/manifest.json").read_text(encoding="utf-8"))
    run_metadata = manifest["run_metadata"]
    run_metadata["cases"] = {case_id: run_metadata["cases"][case_id]}
    run_metadata["usage"] = {case_id: run_metadata["usage"][case_id]}
    run_metadata["policy_arm"] = arm
    run_metadata["policy_arm_digest"] = None if arm == "none" else "sha256-v1:" + "0" * 64
    manifest["responses"] = {case_id: f"responses/{case_id}.json"}
    manifest["prompt_version"] = prompt_version
    (responses / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_lossless_deviations_are_invalid_in_strict_and_valid_after_normalization(
    tmp_path: Path,
) -> None:
    unsorted_with_equal_range = answer(
        answer_finding(line=9, severity="low", category="readability"),
        answer_finding(start_line=3),
    )

    report = replay(one_case_capture(tmp_path, unsorted_with_equal_range))

    assert report["valid_response_count"] == 0
    assert report["micro"]["tp"] == 0
    assert report["verdict"]["agreed"] == 0
    normalized = report["gateway_normalized"]
    assert normalized["valid_response_count"] == 1
    assert normalized["validity"] == 1.0
    assert (normalized["micro"]["tp"], normalized["micro"]["fp"]) == (1, 1)
    assert normalized["critical"]["matched"] == 1
    assert normalized["verdict"]["agreed"] == 1


def test_a_real_error_is_invalid_in_both_columns(tmp_path: Path) -> None:
    reversed_range = answer(answer_finding(start_line=4, rule_name="Naming Consistency"))

    report = replay(one_case_capture(tmp_path, reversed_range))

    assert report["valid_response_count"] == 0
    normalized = report["gateway_normalized"]
    assert normalized["valid_response_count"] == 0
    assert normalized["micro"]["fn"] == 1
    assert normalized["attribution_loss"] == {
        "findings": 0,
        "rule_name_nulled": 0,
        "custom_rule_sets": [],
    }


def test_attribution_loss_counts_what_the_post_processor_would_null(tmp_path: Path) -> None:
    python_case = answer(
        answer_finding(rule_name="Naming Consistency", body="No prefix."),
        answer_finding(rule_name="FSD Layer Boundaries", body=attributed("FSD Layer Boundaries")),
        answer_finding(rule_name="Invented", body=attributed("Invented")),
        answer_finding(rule_name="Naming Consistency", body=attributed("Error Handling Standards")),
        # Kept: a prefix of a rule of the case's language, and no rule claimed at all.
        answer_finding(rule_name="Naming Consistency", body=attributed("Naming Consistency")),
        answer_finding(rule_name=None, body="No rule claimed."),
    )

    report = replay(one_case_capture(tmp_path, python_case))

    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 6,
        "rule_name_nulled": 4,
        "custom_rule_sets": [],
    }


def test_attribution_loss_uses_the_rule_set_of_the_case_language(tmp_path: Path) -> None:
    typescript_case = answer(
        answer_finding(rule_name="FSD Layer Boundaries", body=attributed("FSD Layer Boundaries")),
        answer_finding(
            rule_name="Clean Architecture Boundaries",
            body=attributed("Clean Architecture Boundaries"),
        ),
    )

    report = replay(one_case_capture(tmp_path, typescript_case, case_id="SYN-04"))

    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 2,
        "rule_name_nulled": 1,
        "custom_rule_sets": [],
    }


def snapshot_finding(rule_name: str | None) -> dict[str, Any]:
    """A ReviewOutput v2 finding, the answer of a snapshot-path prompt."""
    return {
        **answer_finding(rule_name=rule_name, body="No v1 prefix on the snapshot path."),
        "evidence_refs": [{"block_id": "file-app-authorization", "start_line": 3, "end_line": 3}],
    }


SNAPSHOT_ANSWER = answer(
    snapshot_finding("service-defaults:C1"),
    snapshot_finding(".review/rules.md"),
    snapshot_finding("AGENTS.md"),
    snapshot_finding(None),
)


def test_snapshot_path_keeps_service_defaults_and_nulls_absent_documents(tmp_path: Path) -> None:
    report = replay(one_case_capture(tmp_path, SNAPSHOT_ANSWER, prompt_version="v5", arm="none"))

    assert report["gateway_normalized"]["valid_response_count"] == 1
    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 4,
        "rule_name_nulled": 2,
        "custom_rule_sets": [],
    }


def test_snapshot_path_keeps_the_documents_the_case_shows(tmp_path: Path) -> None:
    root = one_case_capture(tmp_path, SNAPSHOT_ANSWER, prompt_version="v5", arm="valid")
    (root / "cases/SEC-01/base/AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    report = replay(root)

    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 4,
        "rule_name_nulled": 0,
        "custom_rule_sets": [],
    }


def test_a_strict_valid_answer_scores_the_same_in_both_columns(tmp_path: Path) -> None:
    report = replay(one_case_capture(tmp_path, answer(answer_finding())))

    for column in (report, report["gateway_normalized"]):
        assert column["valid_response_count"] == 1
        assert (column["micro"]["tp"], column["micro"]["fp"], column["micro"]["fn"]) == (1, 0, 0)
        assert (column["critical"]["matched"], column["critical"]["total"]) == (1, 1)
        assert (column["verdict"]["agreed"], column["verdict"]["total"]) == (1, 1)


def rule_set_repo(tmp_path: Path, rule_set: str | None) -> Path:
    """A prompt root whose backend rule set is this text; ``None`` leaves the file missing."""
    repo = tmp_path / "repo"
    (repo / "review/rules").mkdir(parents=True)
    if rule_set is not None:
        (repo / "review/rules/default-backend.v1.json").write_text(rule_set, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    "rule_set",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param('{"rules": "Naming Consistency"}', id="rules-not-a-list"),
        pytest.param('{"rules": [{"title": "No name"}]}', id="rule-without-a-name"),
    ],
)
def test_a_malformed_rule_set_stops_the_replay_instead_of_inflating_the_count(
    tmp_path: Path, rule_set: str
) -> None:
    root = one_case_capture(tmp_path, answer(answer_finding()))

    with pytest.raises(ReplayError, match="default-backend.v1.json"):
        replay(root, prompt_root=rule_set_repo(tmp_path, rule_set))


def test_a_missing_rule_set_is_a_warning_and_counts_every_attribution_as_lost(
    tmp_path: Path,
) -> None:
    known_rule = answer(
        answer_finding(rule_name="Naming Consistency", body=attributed("Naming Consistency"))
    )

    report = replay(
        one_case_capture(tmp_path, known_rule), prompt_root=rule_set_repo(tmp_path, None)
    )

    assert any(warning.startswith("Static input") for warning in report["warnings"])
    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 1,
        "rule_name_nulled": 1,
        "custom_rule_sets": [],
    }


def pin_rule_sets(tmp_path: Path, root: Path, rule_sets: dict[str, str]) -> Path:
    """Add these rule files to the capture's static inputs; return a prompt root that holds them."""
    manifest_path = root / "responses/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["static_inputs"] = sorted({*manifest["static_inputs"], *rule_sets})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "review/rules").mkdir(parents=True)
    for default in ("default-backend.v1.json", "default-frontend.v1.json"):
        shutil.copy2(REPO_ROOT / "review/rules" / default, repo / "review/rules" / default)
    for path, text in rule_sets.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    return repo


CUSTOM_RULE_SET = json.dumps(
    {"version": 1, "stack": "backend", "rules": [{"name": "Custom Source Review"}]}
)
CUSTOM_ATTRIBUTION = answer(
    answer_finding(rule_name="Custom Source Review", body=attributed("Custom Source Review"))
)


# eval_live pins a --backend-rules or --frontend-rules path in static_inputs and nothing tells
# which flag chose it: test_selected_custom_rules_outside_default_rule_glob_are_hashed.
@pytest.mark.parametrize(
    "paths",
    [
        pytest.param(["config/custom.json"], id="outside-the-rule-directory"),
        pytest.param(["config/backend.json", "config/frontend.json"], id="both-flags"),
        pytest.param(["config/custom.rules"], id="any-file-name"),
        pytest.param(["review/rules/default-backend.v2.json"], id="inside-the-rule-directory"),
    ],
)
def test_a_custom_rule_set_leaves_the_attribution_loss_uncounted_instead_of_guessing(
    tmp_path: Path, paths: list[str]
) -> None:
    root = one_case_capture(tmp_path, CUSTOM_ATTRIBUTION)
    repo = pin_rule_sets(tmp_path, root, dict.fromkeys(paths, CUSTOM_RULE_SET))

    report = replay(root, prompt_root=repo)

    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 1,
        "rule_name_nulled": None,
        "custom_rule_sets": paths,
    }
    assert report["gateway_normalized"]["valid_response_count"] == 1
    assert report["valid_response_count"] == 1


def test_a_pinned_json_without_rules_is_not_a_rule_set(tmp_path: Path) -> None:
    root = one_case_capture(
        tmp_path,
        answer(
            answer_finding(rule_name="Naming Consistency", body=attributed("Naming Consistency"))
        ),
    )
    schema = (REPO_ROOT / "review/rules/schema.json").read_text(encoding="utf-8")
    repo = pin_rule_sets(tmp_path, root, {"review/rules/schema.json": schema})

    report = replay(root, prompt_root=repo)

    assert report["gateway_normalized"]["attribution_loss"] == {
        "findings": 1,
        "rule_name_nulled": 0,
        "custom_rule_sets": [],
    }


def test_console_says_why_the_attribution_loss_is_not_counted(tmp_path: Path) -> None:
    root = one_case_capture(tmp_path, CUSTOM_ATTRIBUTION)
    repo = pin_rule_sets(tmp_path, root, {"config/custom.json": CUSTOM_RULE_SET})

    lines = format_console(replay(root, prompt_root=repo)).splitlines()

    start = lines.index("  rule_name nulled by the post-processor: unknown/1 findings")
    assert lines[start + 1] == (
        "  not counted: the manifest does not tie these rule sets to a language: config/custom.json"
    )


def test_console_shows_the_second_column_as_a_block_not_the_published_review(
    tmp_path: Path,
) -> None:
    capture = answer(
        answer_finding(line=9, severity="low", category="readability"),
        answer_finding(start_line=3, rule_name="Naming Consistency"),
    )

    lines = format_console(replay(one_case_capture(tmp_path, capture))).splitlines()

    assert "Validity: 0.0% (0/1)" in lines
    start = lines.index(
        "After gateway normalization, before lint, dedupe, threshold, anchors and verifier "
        "(not the published review):"
    )
    assert lines[start + 1 : start + 6] == [
        "  Validity: 100.0% (1/1)",
        "  Micro: TP=1 FP=1 FN=0 Precision=50.0% Recall=100.0%",
        "  Critical Recall: 100.0% (1/1)",
        "  Verdict agreement: 100.0% (1/1)",
        "  rule_name nulled by the post-processor: 1/2 findings",
    ]
    # The narrowed corpus differs from the committed digest, so this capture has a warning.
    assert not [line for line in lines[:start] if line.startswith("Warning:")]
    assert [line for line in lines[start:] if line.startswith("Warning:")]


def test_console_of_a_report_without_the_second_column_has_no_block(tmp_path: Path) -> None:
    report = replay(one_case_capture(tmp_path, answer(answer_finding())))
    del report["gateway_normalized"]

    assert "gateway normalization" not in format_console(report)
