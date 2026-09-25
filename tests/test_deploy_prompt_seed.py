"""Deployment contracts for the post-migration prompt bootstrap."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_image_carries_versioned_prompt_assets_and_can_seed_without_uv() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text(encoding="utf-8")

    assert "COPY review/prompts ./review/prompts" in dockerfile
    assert "COPY review/postprocess ./review/postprocess" in dockerfile
    assert "COPY review/rules ./review/rules" in dockerfile
    assert "rm -f /bin/uv /bin/uvx" in dockerfile
    assert "Smoke-test packaged review artifacts" in workflow
    assert "FindingsPostProcessor.from_default_patterns()" in workflow
    assert 'load_default_rule_sets(Path("review/rules"))' in workflow


def test_staging_bootstraps_migrations_then_prompts_before_starting_api() -> None:
    compose = (REPO_ROOT / "deploy" / "compose" / "staging.yml").read_text(encoding="utf-8")
    deploy_script = (REPO_ROOT / "deploy" / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "bootstrap:" in compose
    assert '"alembic upgrade head && python -m app.bootstrap.seed_prompts"' in compose
    assert "condition: service_completed_successfully" in compose
    assert "DATABASE_URL: postgresql+psycopg://" in compose
    assert compose.index("alembic upgrade head") < compose.index(
        "python -m app.bootstrap.seed_prompts"
    )
    assert '"${COMPOSE[@]}" rm -sf bootstrap' in deploy_script
