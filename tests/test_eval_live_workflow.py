"""Exercise manual eval shell steps without credentials or provider calls."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/eval-live.yml"


def _step(name: str) -> str:
    text = WORKFLOW.read_text()
    block = text.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    match = re.search(r"(?m)^        run: \|\n(?P<body>(?:^          .*\n|^\n)*)", block)
    assert match is not None
    return textwrap.dedent(match.group("body"))


def _run(name: str, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", _step(name)],
        cwd=root,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_prepare_live_corpus_preserves_committed_responses(tmp_path: Path) -> None:
    dataset = tmp_path / "test-prs-dataset"
    for directory in ("cases/SEC-01", "schema", "responses"):
        (dataset / directory).mkdir(parents=True)
    (dataset / "cases/SEC-01/diff.patch").write_bytes(b"patch\n")
    (dataset / "schema/case.schema.json").write_text("{}")
    response = dataset / "responses/SEC-01.json"
    response.write_bytes(b"original raw answer")
    (dataset / "responses/manifest.json").write_text("{}")
    runner = tmp_path / "runner"
    runner.mkdir()

    result = _run("Prepare isolated corpus", tmp_path, {"RUNNER_TEMP": str(runner)})

    assert result.returncode == 0, result.stderr
    assert response.read_bytes() == b"original raw answer"
    assert (runner / "eval-corpus/cases/SEC-01/diff.patch").read_bytes() == b"patch\n"
    assert (runner / "eval-corpus/schema/case.schema.json").read_text() == "{}"
    assert not (runner / "eval-corpus/responses").exists()


def test_live_failure_is_not_hidden_and_diagnostics_remain(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" > "$RUNNER_TEMP/args"\n'
        'printf "{}" > "$RUNNER_TEMP/eval-report.json"\n'
        "exit 27\n"
    )
    shim.chmod(0o755)
    result = _run(
        "Evaluate live corpus",
        tmp_path,
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "LLM_API_KEYS": "test-key-not-a-real-credential",
        },
    )

    assert result.returncode == 27
    assert (tmp_path / "eval-report.json").read_text() == "{}"
    assert (tmp_path / "args").read_text().splitlines() == [
        "run",
        "python",
        "review/scripts/eval_live.py",
        "--root",
        str(tmp_path / "eval-corpus"),
        "--report-json",
        str(tmp_path / "eval-report.json"),
        "--redacted-responses",
        str(tmp_path / "eval-response-metadata"),
    ]
    assert "test-key-not-a-real-credential" not in result.stdout + result.stderr


def test_missing_key_fails_before_provider_call(tmp_path: Path) -> None:
    shim = tmp_path / "uv"
    shim.write_text('#!/bin/bash\n: > "$RUNNER_TEMP/uv-called"\nexit 42\n')
    shim.chmod(0o755)
    result = _run(
        "Evaluate live corpus",
        tmp_path,
        {"RUNNER_TEMP": str(tmp_path), "LLM_API_KEYS": "", "PATH": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "AI_DMC268_T6" in result.stderr
    assert not (tmp_path / "uv-called").exists()
    assert not (tmp_path / "eval-report.json").exists()
    # Positive control: the same PATH-restricted shim must mark an invocation.
    control = _run(
        "Evaluate live corpus",
        tmp_path,
        {"RUNNER_TEMP": str(tmp_path), "LLM_API_KEYS": "test-key", "PATH": str(tmp_path)},
    )
    assert control.returncode == 42
    assert (tmp_path / "uv-called").exists()


def test_summary_uses_replay_metrics_without_model_diagnostics(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text('#!/bin/bash\nexec "$PYTHON_EXE" -\n')
    shim.chmod(0o755)
    report = {
        "case_count": 24,
        "valid_response_count": 16,
        "validity": 16 / 24,
        "provenance": {
            "model_id": "model<test>",
            "prompt_path": "review/prompts/review.system.v2.md",
            "prompt_version": "v2",
            "prompt_sha": "a" * 64,
            "static_digest": "sha256-v1:" + "b" * 64,
            "corpus_digest": "sha256-v1:" + "c" * 64,
        },
        "micro": {"tp": 3, "fp": 5, "fn": 16, "precision": 0.375, "recall": 3 / 19},
        "critical": {"matched": 2, "total": 5, "recall": 0.4},
        "per_category": {"security": {"tp": 1, "fp": 0, "fn": 3, "precision": 1.0, "recall": 0.25}},
        "verdict": {"agreed": 7, "total": 24, "agreement": 7 / 24},
        "severity_mismatches": [],
        "warnings": ["Recorded capture is not publishable"],
        "responses": [{"validator_output": "RAW MODEL TEXT MUST NOT APPEAR"}],
    }
    (tmp_path / "eval-report.json").write_text(json.dumps(report))
    summary = tmp_path / "summary.md"
    result = _run(
        "Summarize evaluation",
        tmp_path,
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHON_EXE": sys.executable,
            "PYTHONPATH": str(REPO_ROOT),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    assert result.returncode == 0, result.stderr
    content = summary.read_text()
    assert "Model: model&lt;test&gt;" in content
    assert "Validity: 66.7% (16/24)" in content
    assert "Precision=37.5% Recall=15.8%" in content
    assert "security: TP=1 FP=0 FN=3 Precision=100.0% Recall=25.0%" in content
    assert "Verdict agreement: 29.2% (7/24)" in content
    assert "Warning: Recorded capture is not publishable" in content
    assert "RAW MODEL TEXT MUST NOT APPEAR" not in content


def _assert_workflow_contract(text: str) -> None:
    _assert_raw_export_contract(text)
    trigger = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", trigger, re.M) == ["workflow_dispatch"]
    assert "      model:\n" in trigger
    assert "        required: false\n" in trigger
    assert "        type: string\n" in trigger
    assert "permissions:\n  contents: read\n\n" in text
    assert "LLM_MODEL: ${{ inputs.model || vars.LLM_MODEL }}" in text
    assert text.count("${{ secrets.") == 1
    evaluate = text.split("      - name: Evaluate live corpus\n")[1].split("\n      - name:")[0]
    assert "${{ inputs.model" not in evaluate
    assert "          LLM_API_KEYS: ${{ secrets.AI_DMC268_T6 }}" in evaluate
    assert 'validate_dataset.py --root "$RUNNER_TEMP/eval-corpus" --final' in text
    for name in ("Export safe provenance", "Summarize evaluation", "Upload evaluation report"):
        assert f"      - name: {name}\n        if: always()\n" in text
    upload = text.split("      - name: Upload evaluation report\n")[1]
    paths = upload.split("          path: |\n")[1].split("          if-no-files-found:")[0]
    assert paths.splitlines() == [
        "            ${{ runner.temp }}/eval-report.json",
        "            ${{ runner.temp }}/eval-response-metadata/",
    ]
    assert "          retention-days: 14\n" in upload


def test_manual_workflow_security_and_selection_contract() -> None:
    _assert_workflow_contract(WORKFLOW.read_text())


@pytest.mark.parametrize(
    "before,after",
    [
        ("on:\n", "on:\n  push:\n"),
        ("default: false", "default: true"),
        ("type: boolean", "type: string"),
        ("always() && inputs.export_raw_responses", "always()"),
        (" && steps.raw_export.outcome == 'success'", ""),
        ("path: ${{ runner.temp }}/eval-raw-responses/", "path: ${{ runner.temp }}/"),
        ("contents: read", "contents: write"),
        ("if: always()", "if: success()"),
        ("retention-days: 14", "retention-days: 90"),
        ("/eval-response-metadata/", "/eval-corpus/"),
        ('--root "$RUNNER_TEMP/eval-corpus" --final', "--final"),
        ("${{ inputs.model || vars.LLM_MODEL }}", "${{ vars.LLM_MODEL }}"),
        ("    env:\n", "    env:\n      EXTRA_KEY: ${{ secrets.AI_DMC268_T6 }}\n"),
    ],
)
def test_workflow_contract_rejects_unsafe_mutations(before: str, after: str) -> None:
    original = WORKFLOW.read_text()
    assert before in original
    with pytest.raises(AssertionError):
        _assert_workflow_contract(original.replace(before, after))


def test_safe_provenance_exports_only_generated_manifest(tmp_path: Path) -> None:
    responses = tmp_path / "eval-corpus/responses"
    responses.mkdir(parents=True)
    manifest = {
        "run_metadata": {
            "recorded_at": "2026-10-07T12:00:00Z",
            "fallback_model_id": "fallback-model",
            "effective_settings": {"context_window": 32768},
            "cases": {
                "SEC-01": {"first_provider_label": "scaleway", "first_model": "primary-model"}
            },
        }
    }
    (responses / "manifest.json").write_text(json.dumps(manifest))
    (responses / "SEC-01.json").write_text("RAW_MODEL_TEXT_MUST_NOT_UPLOAD")
    result = _run("Export safe provenance", tmp_path, {"RUNNER_TEMP": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    exported = tmp_path / "eval-response-metadata"
    assert [path.name for path in exported.iterdir()] == ["manifest.json"]
    assert json.loads((exported / "manifest.json").read_text()) == manifest


def test_missing_provenance_preserves_failed_run_summary(tmp_path: Path) -> None:
    result = _run("Export safe provenance", tmp_path, {"RUNNER_TEMP": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "eval-response-metadata").exists()


def _assert_raw_export_contract(text: str) -> None:
    assert (
        "      export_raw_responses:\n"
        '        description: "Export raw first answers and manifest for baseline publication"\n'
        "        required: false\n"
        "        type: boolean\n"
        "        default: false\n"
    ) in text
    assert (
        "      - name: Stage raw responses\n"
        "        id: raw_export\n"
        "        if: ${{ always() && inputs.export_raw_responses }}\n"
    ) in text
    assert (
        "      - name: Upload raw responses\n"
        "        if: ${{ always() && inputs.export_raw_responses"
        " && steps.raw_export.outcome == 'success' }}\n"
    ) in text
    upload = text.split("      - name: Upload raw responses\n", 1)[1]
    assert "          path: ${{ runner.temp }}/eval-raw-responses/\n" in upload
    assert "          retention-days: 14\n" in upload
    assert "          if-no-files-found: error\n" in upload
    assert text.count("uses: actions/upload-artifact@") == 2


def _raw_capture(root: Path) -> Path:
    responses = root / "eval-corpus/responses"
    responses.mkdir(parents=True)
    for case_id, content in (("SEC-01", b""), ("SEC-02", b"invalid\r\nanswer\x00")):
        case = root / "eval-corpus/cases" / case_id
        case.mkdir(parents=True)
        (case / "case.json").write_text("{}")
        (responses / f"{case_id}.json").write_bytes(content)
    (responses / "manifest.json").write_text(
        '{"responses":{"SEC-01":"responses/SEC-01.json","SEC-02":"responses/SEC-02.json"}}\n'
    )
    (responses / "unexpected.json").write_text("DO NOT EXPORT")
    (responses / "diagnostics.log").write_text("DO NOT EXPORT")
    return responses


def _export_raw(root: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text('#!/bin/bash\nexec "$PYTHON_EXE" -\n')
    shim.chmod(0o755)
    return _run(
        "Stage raw responses",
        root,
        {
            "RUNNER_TEMP": str(root),
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHON_EXE": sys.executable,
        },
    )


def test_raw_export_preserves_only_mapped_answer_and_manifest_bytes(tmp_path: Path) -> None:
    responses = _raw_capture(tmp_path)
    result = _export_raw(tmp_path)
    assert result.returncode == 0, result.stderr
    exported = tmp_path / "eval-raw-responses"
    assert sorted(path.name for path in exported.iterdir()) == [
        "SEC-01.json",
        "SEC-02.json",
        "manifest.json",
    ]
    assert (exported / "SEC-01.json").read_bytes() == b""
    assert (exported / "SEC-02.json").read_bytes() == b"invalid\r\nanswer\x00"
    assert (exported / "manifest.json").read_bytes() == (responses / "manifest.json").read_bytes()


@pytest.mark.parametrize("unsafe", ["traversal", "extra", "missing", "symlink", "manifest_symlink"])
def test_raw_export_rejects_unsafe_or_incomplete_capture(tmp_path: Path, unsafe: str) -> None:
    responses = _raw_capture(tmp_path)
    manifest = responses / "manifest.json"
    data = json.loads(manifest.read_text())
    if unsafe == "traversal":
        data["responses"]["SEC-01"] = "responses/../private.json"
        (responses.parent / "private.json").write_text("PRIVATE")
    elif unsafe == "extra":
        data["responses"]["unexpected"] = "responses/unexpected.json"
    elif unsafe == "missing":
        del data["responses"]["SEC-01"]
    elif unsafe == "symlink":
        (responses / "SEC-01.json").unlink()
        (responses / "SEC-01.json").symlink_to(responses / "unexpected.json")
    manifest.write_text(json.dumps(data))
    if unsafe == "manifest_symlink":
        target = responses / "other.json"
        manifest.rename(target)
        manifest.symlink_to(target)
    result = _export_raw(tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "eval-raw-responses").exists()
