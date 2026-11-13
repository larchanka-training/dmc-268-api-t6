"""Consistency verification for error codes degradation catalog in PIPELINE_SPEC.md (Refs #461)."""

from __future__ import annotations

from pathlib import Path

from app.modules.reviews.application.run_failures import (
    KNOWN_ERROR_CODES,
    RETRYABLE_POLICY_ERROR_CODES,
    TERMINAL_POLICY_ERROR_CODES,
)

ROOT = Path(__file__).resolve().parent.parent


def _load_catalog_table() -> dict[str, dict[str, str]]:
    spec_text = (ROOT / "docs" / "PIPELINE_SPEC.md").read_text(encoding="utf-8")
    assert "### 6.2 Матрица нештатных условий" in spec_text
    after_header = spec_text.split("### 6.2 Матрица нештатных условий", 1)[1]
    section = after_header.split("## 7. Check-run бота", 1)[0]

    rows: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        if line.startswith("| `") and "` |" in line:
            parts = [p.strip() for p in line.split("|")[1:-1]]
            assert len(parts) == 4, f"Row has {len(parts)} parts, expected 4: {line}"
            code = parts[0].strip("` ")
            if code == "error_code":
                continue
            assert code not in rows, f"Duplicate error code in catalog: {code}"
            rows[code] = {
                "code": code,
                "category": parts[1],
                "strategy": parts[2],
                "explanation": parts[3],
            }
    return rows


def test_catalog_table_contains_all_known_error_codes() -> None:
    table = _load_catalog_table()
    table_codes = set(table.keys())
    missing = KNOWN_ERROR_CODES - table_codes
    extra = table_codes - KNOWN_ERROR_CODES
    assert table_codes == KNOWN_ERROR_CODES, f"Missing: {missing}, Extra: {extra}"


def test_every_code_has_defined_strategy() -> None:
    table = _load_catalog_table()
    for code, row in table.items():
        strategy = row["strategy"]
        assert strategy in {"падаем", "деградируем"}, (
            f"Code {code} has undefined strategy: {strategy!r}"
        )
        assert len(row["explanation"]) > 20, (
            f"Code {code} has too short explanation: {row['explanation']}"
        )


def test_llm_context_overflow_degradation_specified() -> None:
    table = _load_catalog_table()
    row = table["llm_context_overflow"]
    assert row["strategy"] == "деградируем"
    assert "NoModelAnswer" in row["explanation"]
    assert "review_input_incomplete" in row["explanation"]


def test_bitbucket_budget_degradation_specified() -> None:
    table = _load_catalog_table()
    for code in ["bitbucket_request_budget_exceeded", "bitbucket_response_budget_exceeded"]:
        row = table[code]
        assert row["strategy"] == "деградируем"
        assert "review_context_stage.py" in row["explanation"]
        assert "vcs_diff.py" in row["explanation"]


def test_policy_codes_all_terminal_fail() -> None:
    table = _load_catalog_table()
    all_policy_codes = TERMINAL_POLICY_ERROR_CODES | RETRYABLE_POLICY_ERROR_CODES
    for code in all_policy_codes:
        row = table[code]
        assert row["strategy"] == "падаем", f"Policy code {code} must fail"


def test_verification_codes_all_fail_to_preserve_safety() -> None:
    table = _load_catalog_table()
    verification_codes = [c for c in KNOWN_ERROR_CODES if c.startswith("verification_")]
    expected_count = len([c for c in KNOWN_ERROR_CODES if c.startswith("verification_")])
    assert len(verification_codes) == expected_count
    for code in verification_codes:
        row = table[code]
        assert row["strategy"] == "падаем", f"Verification code {code} must fail"
