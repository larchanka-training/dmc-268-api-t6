"""Bitbucket's published contracts describe the supported review path and settings."""

from pathlib import Path

from openapi_spec_validator.readers import read_from_filename

from app.modules.reviews.application.review_path import (
    SNAPSHOT_REVIEW_PROMPT_VERSIONS,
    SNAPSHOT_REVIEW_PROVIDERS,
)

ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_and_rag_docs_cover_bitbucket_with_the_common_active_prompt() -> None:
    assert "bitbucket" in SNAPSHOT_REVIEW_PROVIDERS
    assert "v4" in SNAPSHOT_REVIEW_PROMPT_VERSIONS
    assert "v5" in SNAPSHOT_REVIEW_PROMPT_VERSIONS

    system_design = (ROOT / "docs/SYSTEM_DESIGN.md").read_text()
    pipeline = (ROOT / "docs/PIPELINE_SPEC.md").read_text()

    assert "(GitHub, GitLab и Bitbucket Cloud)" in system_design
    assert "Bitbucket run закрепляет общую активную версию `review.system`" in system_design
    assert "После #350 активен общий v5" in system_design
    assert "GitHub, GitLab и Bitbucket Cloud runs закрепляют общую активную версию" in pipeline
    assert "после #350 это v5" in pipeline
    assert "закреплённому v4 или v5; runs с v3 или ниже" in pipeline
    assert "`context.manifest` — только snapshot-путь с закреплённым v4 или v5" in pipeline
    assert (
        "`context.rag` — только при включённом тумблере RAG доступного репозитория "
        "GitHub или Bitbucket Cloud"
    ) in pipeline


def test_current_runtime_doc_rows_name_common_v5_and_historical_v4() -> None:
    pipeline_lines = (ROOT / "docs/PIPELINE_SPEC.md").read_text().splitlines()
    system_lines = (ROOT / "docs/SYSTEM_DESIGN.md").read_text().splitlines()

    metadata = next(
        line for line in pipeline_lines if line.startswith("| Машиночитаемые контракты |")
    )
    severity = next(
        line for line in pipeline_lines if line.startswith("**Порог severity репозитория")
    )
    finding = next(line for line in pipeline_lines if line.startswith("| Finding «от Теклида»"))
    worker = next(line for line in system_lines if line.startswith("- Worker собирает LLM Gateway"))

    assert "F2 seed активировал v4, а после #350 для новых runs активен общий v5" in metadata
    assert "F2 seed активировал v4, а после #350 новые runs закрепляют общий v5" in severity
    assert "verifier подключена #130 F1 для закреплённого v4 или v5" in finding
    assert "Snapshot-путь (run на v4 или v5):" in worker


def test_test_plan_keeps_v4_rules_fixture_historical_and_new_runs_on_v5() -> None:
    plan = " ".join((ROOT / "docs/TEST_PLAN.md").read_text().split())

    assert (
        "F2 seed активировал `review.system:v4` для новых runs; "
        "после #350 новые runs закрепляют общий v5."
    ) in plan
    assert "Snapshot-путь выбирается по закреплённому v4 или v5" in plan
    assert (
        "С #130 F2 (PR #332) новые GitHub runs закрепляли `review.system` v4; "
        "после #350 новые runs закрепляют общий v5."
    ) in plan
    assert "таблица А проверяет это на историческом seed `[v4]`" in plan
    assert "с закреплённым `review.system:v4` или `review.system:v5`" in plan
    assert (
        "После F2 seed v4 был активен для новых runs; после #350 новые runs закрепляют общий v5."
    ) in plan


def test_bitbucket_unsupported_settings_have_documented_422_shape_and_codes() -> None:
    spec, _ = read_from_filename(str(ROOT / "contracts/openapi.yaml"))
    response = spec["paths"]["/api/repos/{repo_id}"]["patch"]["responses"]["422"]
    description = response["description"]
    examples = response["content"]["application/json"]["examples"]

    for field, code in (
        ("waitForCi", "bitbucket_wait_for_ci_unsupported"),
        ("reviewEvent", "bitbucket_review_event_unsupported"),
    ):
        assert code in description
        assert examples[field]["value"] == {
            "detail": [{"loc": ["body", field], "msg": code, "type": "value_error"}]
        }
    assert "bitbucket_wait_for_ci_unsupported" in (ROOT / "docs/SYSTEM_DESIGN.md").read_text()
    assert "bitbucket_review_event_unsupported" in (ROOT / "docs/SYSTEM_DESIGN.md").read_text()


def test_pipeline_trace_table_keeps_upstream_manifest_and_bitbucket_rows() -> None:
    pipeline = (ROOT / "docs/PIPELINE_SPEC.md").read_text()
    trace_table = pipeline.split("| `tool` | `request`", 1)[1].split("\n\n", 1)[0]

    assert all(line.startswith("|") for line in trace_table.splitlines()[1:])
    assert "`retrieved_excerpt`" in trace_table
    assert "| `context.rag` |" in trace_table
    assert "| `context.manifest` — только snapshot-путь с закреплённым v4 или v5" in trace_table
    assert "| `bitbucket.publish_review`" in trace_table
    assert "на `main` — `{outcome: not_ready" not in trace_table
