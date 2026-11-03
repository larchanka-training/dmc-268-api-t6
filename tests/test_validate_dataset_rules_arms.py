"""Case validator: the rules.md sub-corpus - policy arm files, ignore masks, malicious arm."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.validate_dataset_fakes import (
    INJECTION,
    RULES_PATCH,
    RULES_TEXT,
    _drop,
    _rules_case,
    _rules_metadata,
    _run,
)

# --- #163: rules.md sub-corpus (policy arms) -------------------------------------------------


def test_rules_case_with_policy_arms_passes(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "OK RUL-01\n"


def _truth_change(index: int, **fields: object) -> Any:
    def apply(record: dict[str, Any]) -> None:
        record["expected_findings"][index].update(fields)

    return apply


def _exception_change(**fields: object) -> Any:
    def apply(record: dict[str, Any]) -> None:
        record["must_not_flag"][0].update(fields)

    return apply


@pytest.mark.parametrize(
    ("change", "diagnostic"),
    [
        (
            _truth_change(0, arms=[]),
            "RUL-01: case.json.expected_findings.0.arms: [] should be non-empty",
        ),
        (
            _truth_change(0, arms=["rag"]),
            "RUL-01: case.json.expected_findings.0.arms.0: "
            "'rag' is not one of ['none', 'valid', 'malicious']",
        ),
        (
            _truth_change(0, arms=["none", "none"]),
            "RUL-01: case.json.expected_findings.0.arms: ['none', 'none'] has non-unique elements",
        ),
        (
            _drop("expected_findings", 1, "rule_ref"),
            "RUL-01: case.json.expected_findings.1: 'rule_ref' is a required property",
        ),
        (
            _truth_change(1, origin="generic"),
            "should not be valid under {'required': ['rule_ref']}",
        ),
        (
            _drop("must_not_flag", 0, "reason"),
            "RUL-01: case.json.must_not_flag.0: 'reason' is a required property",
        ),
        (
            _exception_change(severity="high"),
            "RUL-01: case.json.must_not_flag.0: Additional properties are not allowed "
            "('severity' was unexpected)",
        ),
        (_drop("policy"), "RUL-01: case.json.$: 'policy' is a required property"),
        (
            _drop("policy", "arms", "malicious"),
            "RUL-01: case.json.policy.arms: 'malicious' is a required property",
        ),
        (_truth_change(0, origin="rule2"), "'rule2' is not one of ['rule', 'generic']"),
    ],
    ids=[
        "empty-arms",
        "unknown-arm",
        "duplicate-arm",
        "rule-without-ref",
        "ref-without-rule",
        "exception-without-reason",
        "exception-extra-field",
        "rules-class-without-policy",
        "policy-without-malicious",
        "unknown-origin",
    ],
)
def test_schema_rejects_invalid_policy_arm_fields(
    tmp_path: Path, change: Any, diagnostic: str
) -> None:
    metadata = _rules_metadata()
    change(metadata)
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert diagnostic in result.stdout


def test_rules_id_prefix_must_match_rules_class(tmp_path: Path) -> None:
    metadata = _rules_metadata()
    metadata["class"] = "security"
    metadata["expected_findings"][1]["category"] = "security"
    metadata["category_reason"] = "fixture"
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == "RUL-01: case.json.id: prefix does not match class\n"


def test_rules_class_needs_no_category_reason_for_mixed_categories(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)
    metadata = json.loads((dataset / "cases/RUL-01/case.json").read_text(encoding="utf-8"))

    result = _run(dataset, "--case", "RUL-01")

    assert {item["category"] for item in metadata["expected_findings"]} == {
        "security",
        "correctness",
    }
    assert "category_reason" not in metadata
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("malicious", "code"),
    [
        (b"# Rules\n\x01 hidden\n", "policy_document_control_character"),
        (b"# Rules\n\xff\n", "policy_document_decode_error"),
        (b"version https://git-lfs.github.com/spec/v1\noid sha256:0\n", "policy_document_type"),
        (b"```review-ignore\ndist/**\n", "policy_ignore_block_unclosed"),
        (
            b"```review-ignore\ndist/**\n```\n\n```review-ignore\nout/**\n```\n",
            "policy_ignore_block_duplicate",
        ),
        (b"```review-ignore\ndist/[a]\n```\n", "policy_ignore_pattern_invalid"),
        (b"#" * 65_537, "policy_document_too_large"),
    ],
    ids=[
        "control-character",
        "not-utf8",
        "lfs-pointer",
        "unclosed-ignore",
        "duplicate-ignore",
        "invalid-mask",
        "too-large",
    ],
)
def test_arm_file_must_pass_the_production_policy_parsers(
    tmp_path: Path, malicious: bytes, code: str
) -> None:
    dataset = _rules_case(tmp_path, malicious=malicious)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout.startswith(
        f"RUL-01: policy.arms.malicious: .review/rules.malicious.md: {code}"
    )
    assert result.stdout.count("\n") == 1


def test_valid_arm_file_is_checked_like_the_malicious_one(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path)
    (dataset / "cases/RUL-01/base/.review/rules.md").write_bytes(b"## Layers\n\x7f\n")

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout.startswith(
        "RUL-01: policy.arms.valid: .review/rules.md: policy_document_control_character"
    )


def test_case_agents_document_must_pass_the_production_parser(tmp_path: Path) -> None:
    dataset = _rules_case(tmp_path, agents=b"# Conventions\n\x02\n")

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout.startswith(
        "RUL-01: AGENTS.md: AGENTS.md: policy_document_control_character"
    )


@pytest.mark.parametrize(
    "raw", [".review/missing.md", "../case.json", "/etc/hostname"], ids=["missing", "up", "abs"]
)
def test_arm_file_must_exist_under_base_path(tmp_path: Path, raw: str) -> None:
    metadata = _rules_metadata()
    metadata["policy"]["arms"]["malicious"] = raw
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        f"RUL-01: policy.arms.malicious: {raw} is missing or escapes base_path\n"
    )


def test_valid_arm_must_be_the_repository_rules_path(tmp_path: Path) -> None:
    metadata = _rules_metadata()
    metadata["policy"]["arms"]["valid"] = ".review/rules.malicious.md"
    metadata["expected_findings"][1]["rule_ref"] = "## Override"
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.valid: must be .review/rules.md\n"
        "RUL-01: policy.arms.malicious: must name another file than policy.arms.valid\n"
    )


def test_rule_ref_must_occur_in_the_valid_rules_document(tmp_path: Path) -> None:
    metadata = _rules_metadata()
    # Present only in the malicious file: the reference must name a real project rule.
    metadata["expected_findings"][1]["rule_ref"] = "## Override"
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: expected_findings[1].rule_ref: not found in .review/rules.md\n"
    )


def test_exception_reason_must_quote_both_arm_files(tmp_path: Path) -> None:
    metadata = _rules_metadata()
    metadata["must_not_flag"][0]["reason"] = "rules.md exempts the fixture token"
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: must_not_flag[0].reason: not found in .review/rules.md\n"
        "RUL-01: must_not_flag[0].reason: not found in .review/rules.malicious.md\n"
    )


def _ignore_block(*masks: str) -> str:
    return "\n## Generated code\n\n```review-ignore\n" + "".join(f"{m}\n" for m in masks) + "```\n"


def test_ignore_mask_of_the_valid_rules_must_not_match_a_changed_path(tmp_path: Path) -> None:
    # The malicious file repeats every benign section, so it carries the same block.
    rules = RULES_TEXT + _ignore_block("app/*.js", "app/**")
    dataset = _rules_case(tmp_path, rules=rules, malicious=(rules + INJECTION).encode())

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.valid: .review/rules.md: review-ignore mask app/** matches "
        "the changed path app/example.py\n"
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: review-ignore mask "
        "app/** matches the changed path app/example.py\n"
    )


def test_ignore_mask_injected_in_the_malicious_arm_must_not_match_a_changed_path(
    tmp_path: Path,
) -> None:
    malicious = RULES_TEXT + INJECTION + _ignore_block("app/example.py")
    dataset = _rules_case(tmp_path, malicious=malicious.encode())

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: review-ignore mask "
        "app/example.py matches the changed path app/example.py\n"
    )


OLD_PATH_BLOCKS = {
    "rename": (
        "diff --git a/legacy/helpers.py b/app/helpers.py\n"
        "similarity index 100%\nrename from legacy/helpers.py\nrename to app/helpers.py\n"
    ),
    "copy": (
        "diff --git a/legacy/helpers.py b/app/helpers.py\n"
        "similarity index 100%\ncopy from legacy/helpers.py\ncopy to app/helpers.py\n"
    ),
    "delete": (
        "diff --git a/legacy/helpers.py b/legacy/helpers.py\ndeleted file mode 100644\n"
        "--- a/legacy/helpers.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def old():\n-    return 0\n"
    ),
}


@pytest.mark.parametrize("block", list(OLD_PATH_BLOCKS.values()), ids=list(OLD_PATH_BLOCKS))
def test_ignore_mask_must_not_match_the_old_path_of_a_changed_file(
    tmp_path: Path, block: str
) -> None:
    # Production checks both names of a renamed or copied file (apply_ignore).
    rules = RULES_TEXT + _ignore_block("legacy/**")
    dataset = _rules_case(
        tmp_path, rules=rules, malicious=(rules + INJECTION).encode(), patch=RULES_PATCH + block
    )
    legacy = dataset / "cases/RUL-01/base/legacy"
    legacy.mkdir()
    (legacy / "helpers.py").write_text("def old():\n    return 0\n", encoding="utf-8")

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.valid: .review/rules.md: review-ignore mask legacy/** matches "
        "the changed path legacy/helpers.py\n"
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: review-ignore mask "
        "legacy/** matches the changed path legacy/helpers.py\n"
    )


# A block without hunks has no ---/+++ lines: its path comes from the diff --git line.
HUNKLESS_BLOCKS = {
    "empty-new-file": (
        "diff --git a/legacy/empty.py b/legacy/empty.py\nnew file mode 100644\n"
        "index 0000000..e69de29\n",
        {},
        "legacy/empty.py",
    ),
    "mode-only": (
        "diff --git a/legacy/helpers.py b/legacy/helpers.py\nold mode 100644\nnew mode 100755\n",
        {"helpers.py": "def old():\n    return 0\n"},
        "legacy/helpers.py",
    ),
    "empty-delete": (
        "diff --git a/legacy/empty.py b/legacy/empty.py\ndeleted file mode 100644\n"
        "index e69de29..0000000\n",
        {"empty.py": ""},
        "legacy/empty.py",
    ),
    "quoted-header": (
        'diff --git "a/legacy/caf\\303\\251.py" "b/legacy/caf\\303\\251.py"\n'
        "new file mode 100644\nindex 0000000..e69de29\n",
        {},
        "legacy/café.py",
    ),
    # Git splits "a/P b/P" at its middle; a split on the last " b/" would miss the path.
    "name-with-b-slash": (
        "diff --git a/legacy/x b/y.py b/legacy/x b/y.py\nold mode 100644\nnew mode 100755\n",
        {"x b/y.py": "VALUE = 1\n"},
        "legacy/x b/y.py",
    ),
}


@pytest.mark.parametrize(
    ("block", "files", "path"), list(HUNKLESS_BLOCKS.values()), ids=list(HUNKLESS_BLOCKS)
)
def test_ignore_mask_must_not_match_the_path_of_a_block_without_hunks(
    tmp_path: Path, block: str, files: dict[str, str], path: str
) -> None:
    rules = RULES_TEXT + _ignore_block("legacy/**")
    dataset = _rules_case(
        tmp_path, rules=rules, malicious=(rules + INJECTION).encode(), patch=RULES_PATCH + block
    )
    legacy = dataset / "cases/RUL-01/base/legacy"
    legacy.mkdir()
    for name, text in files.items():
        (legacy / name).parent.mkdir(parents=True, exist_ok=True)
        (legacy / name).write_text(text, encoding="utf-8")

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.valid: .review/rules.md: review-ignore mask legacy/** matches "
        f"the changed path {path}\n"
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: review-ignore mask "
        f"legacy/** matches the changed path {path}\n"
    )


RENAME_BLOCKS = {
    # The two names differ, so the diff --git line gives none: the rename lines are decoded.
    "quoted-source": (
        'diff --git "a/legacy/caf\\303\\251.py" b/app/helpers.py\nsimilarity index 100%\n'
        'rename from "legacy/caf\\303\\251.py"\nrename to app/helpers.py\n',
        "legacy/café.py",
        "legacy/**",
        "legacy/café.py",
    ),
    "new-side-only": (
        "diff --git a/legacy/helpers.py b/generated/helpers.py\nsimilarity index 100%\n"
        "rename from legacy/helpers.py\nrename to generated/helpers.py\n",
        "legacy/helpers.py",
        "generated/**",
        "generated/helpers.py",
    ),
}


@pytest.mark.parametrize(
    ("block", "source", "mask", "path"), list(RENAME_BLOCKS.values()), ids=list(RENAME_BLOCKS)
)
def test_ignore_mask_must_not_match_either_name_of_a_rename(
    tmp_path: Path, block: str, source: str, mask: str, path: str
) -> None:
    rules = RULES_TEXT + _ignore_block(mask)
    dataset = _rules_case(
        tmp_path, rules=rules, malicious=(rules + INJECTION).encode(), patch=RULES_PATCH + block
    )
    renamed = dataset / "cases/RUL-01/base" / source
    renamed.parent.mkdir()
    renamed.write_text("def old():\n    return 0\n", encoding="utf-8")

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        f"RUL-01: policy.arms.valid: .review/rules.md: review-ignore mask {mask} matches "
        f"the changed path {path}\n"
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: review-ignore mask "
        f"{mask} matches the changed path {path}\n"
    )


def test_two_masks_on_one_path_give_one_error_per_arm(tmp_path: Path) -> None:
    # The matcher returns the first mask in code point order ("*" < "e"), not in file order.
    rules = RULES_TEXT + _ignore_block("app/example.py", "app/**")
    dataset = _rules_case(tmp_path, rules=rules, malicious=(rules + INJECTION).encode())

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.valid: .review/rules.md: review-ignore mask app/** matches "
        "the changed path app/example.py\n"
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: review-ignore mask "
        "app/** matches the changed path app/example.py\n"
    )


def test_header_like_line_inside_a_hunk_is_not_a_changed_path(tmp_path: Path) -> None:
    # The removed SQL comment reads "--- legacy/report.sql" in the patch.
    rules = RULES_TEXT + _ignore_block("legacy/**")
    patch = RULES_PATCH + (
        "diff --git a/db/report.sql b/db/report.sql\n--- a/db/report.sql\n+++ b/db/report.sql\n"
        "@@ -1,2 +1 @@\n--- legacy/report.sql\n SELECT 1;\n"
    )
    dataset = _rules_case(
        tmp_path, rules=rules, malicious=(rules + INJECTION).encode(), patch=patch
    )
    sql = dataset / "cases/RUL-01/base/db/report.sql"
    sql.parent.mkdir()
    sql.write_text("-- legacy/report.sql\nSELECT 1;\n", encoding="utf-8")

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "OK RUL-01\n"


def test_ignore_mask_check_reports_a_path_the_production_matcher_refuses(
    tmp_path: Path,
) -> None:
    patch = RULES_PATCH + (
        'diff --git "a/app/tab\\there.py" "b/app/tab\\there.py"\nnew file mode 100644\n'
        '--- /dev/null\n+++ "b/app/tab\\there.py"\n@@ -0,0 +1 @@\n+VALUE = 1\n'
    )
    dataset = _rules_case(tmp_path, patch=patch)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.valid: .review/rules.md: the changed path 'app/tab\\there.py' "
        "cannot be matched: policy_path_invalid: A repository path violates the path contract\n"
        "RUL-01: policy.arms.malicious: .review/rules.malicious.md: the changed path "
        "'app/tab\\there.py' cannot be matched: policy_path_invalid: A repository path "
        "violates the path contract\n"
    )


MALICIOUS_WITHOUT_LAYERS = (
    "# Architecture\n\n## Fixtures\n\nThe example token is a fixture\nvalue.\n" + INJECTION
)
MALICIOUS_WITHOUT_FIXTURES = (
    "# Architecture\n\n## Layers\n\nHandlers do not return tokens.\n" + INJECTION
)
MALICIOUS_CHANGED_LAYERS = RULES_TEXT.replace("do not return", "may return") + INJECTION


@pytest.mark.parametrize(
    ("malicious", "diagnostics"),
    [
        (
            MALICIOUS_WITHOUT_LAYERS,
            [
                "policy.arms.malicious: .review/rules.malicious.md lacks the section '## Layers' "
                "of .review/rules.md",
                "expected_findings[1].rule_ref: not found in .review/rules.malicious.md",
            ],
        ),
        (
            MALICIOUS_WITHOUT_FIXTURES,
            [
                "policy.arms.malicious: .review/rules.malicious.md lacks the section "
                "'## Fixtures' of .review/rules.md",
                "must_not_flag[0].reason: not found in .review/rules.malicious.md",
            ],
        ),
        (
            MALICIOUS_CHANGED_LAYERS,
            [
                "policy.arms.malicious: .review/rules.malicious.md lacks the section '## Layers' "
                "of .review/rules.md"
            ],
        ),
        (RULES_TEXT, ["policy.arms.malicious: must differ from the valid rules document"]),
    ],
    ids=["rule-section-dropped", "exception-section-dropped", "section-reworded", "no-injection"],
)
def test_malicious_arm_keeps_every_benign_section_and_adds_an_injection(
    tmp_path: Path, malicious: str, diagnostics: list[str]
) -> None:
    dataset = _rules_case(tmp_path, malicious=malicious.encode())

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == "".join(f"RUL-01: {line}\n" for line in diagnostics)


def test_injection_between_sections_keeps_the_benign_sections(tmp_path: Path) -> None:
    rules_head, fixtures = RULES_TEXT.split("## Fixtures")
    malicious = rules_head + INJECTION.lstrip("\n") + "\n## Fixtures" + fixtures
    dataset = _rules_case(tmp_path, malicious=malicious.encode())

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 0, result.stdout
    assert result.stdout == "OK RUL-01\n"


def test_malicious_arm_must_be_another_file(tmp_path: Path) -> None:
    metadata = _rules_metadata()
    metadata["policy"]["arms"]["malicious"] = ".review/rules.md"
    dataset = _rules_case(tmp_path, metadata)

    result = _run(dataset, "--case", "RUL-01")

    assert result.returncode == 1
    assert result.stdout == (
        "RUL-01: policy.arms.malicious: must name another file than policy.arms.valid\n"
    )
