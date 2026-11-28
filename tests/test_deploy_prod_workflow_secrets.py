"""Deploy prod workflow: production secrets, the bundle and the docs of Environment production."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.deploy_host_fakes import (
    CI_SECRETS,
    ENV_FILES,
    IMAGE_A,
    PROD_PROJECT,
    _read_dotenv,
    _read_env_file,
    _run_script,
    _workflow_step,
    make_host,
)
from tests.deploy_prod_workflow_fakes import (
    BUNDLE,
    CHECKOUT,
    CI_WORKFLOW,
    REFUSE_SECRETS,
    REPO_ROOT,
    UPLOAD,
    VALIDATE,
    _assert_prod_only,
    _deploy_step,
    _docker_calls,
    _job,
    _run,
    _step,
    _step_names,
    _text,
)

SECRETS_DOC = REPO_ROOT / "docs" / "SECRETS.md"


# --- the required production values -----------------------------------------------------------

# GitHub names of what Environment production must hold before the host is touched: seven secrets
# and the bot login variable. The three App values are all-or-nothing for the webhook worker.
REQUIRED_SECRETS = [
    "GH_APP_ID",
    "GH_APP_PRIVATE_KEY",
    "GH_WEBHOOK_SECRET",
    "GH_CLIENT_ID",
    "GH_CLIENT_SECRET",
    "AUTH_JWT_PRIVATE_KEY",
    "AUTH_JWT_PUBLIC_KEY",
]
REQUIRED_NAMES = [*REQUIRED_SECRETS, "GH_APP_BOT_LOGIN"]
ALL_PRESENT = {f"HAS_{name}": "true" for name in REQUIRED_NAMES}


def _refusal(names: str) -> str:
    return f"::error::Environment production lacks required values: {names}; see docs/SECRETS.md\n"


def test_the_refusal_reads_presence_not_values() -> None:
    env = _step(REFUSE_SECRETS).split("        env:\n", 1)[1].split("        run: |\n", 1)[0]
    expected = (
        "".join(
            f"          HAS_{name}: ${{{{ secrets.{name} != '' }}}}\n" for name in REQUIRED_SECRETS
        )
        + "          HAS_GH_APP_BOT_LOGIN: ${{ vars.GH_APP_BOT_LOGIN != '' }}\n"
    )
    assert env == expected


def test_complete_production_secrets_pass(tmp_path: Path) -> None:
    result, output, _ = _run(REFUSE_SECRETS, tmp_path, ALL_PRESENT)

    assert result.returncode == 0, result.stderr
    assert "::error::" not in result.stdout + result.stderr
    assert output == ""


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_a_missing_production_name_is_refused_before_the_host(tmp_path: Path, name: str) -> None:
    result, output, _ = _run(REFUSE_SECRETS, tmp_path, {**ALL_PRESENT, f"HAS_{name}": "false"})

    assert result.returncode == 1
    assert result.stderr == _refusal(name)
    assert output == ""
    assert _docker_calls(tmp_path) == []


def test_all_missing_names_are_listed_in_one_refusal(tmp_path: Path) -> None:
    env = {f"HAS_{name}": "false" for name in REQUIRED_NAMES}

    result, output, _ = _run(REFUSE_SECRETS, tmp_path, env)

    assert result.returncode == 1
    assert result.stderr == _refusal(
        "GH_APP_ID, GH_APP_PRIVATE_KEY, GH_WEBHOOK_SECRET, GH_CLIENT_ID, GH_CLIENT_SECRET,"
        " AUTH_JWT_PRIVATE_KEY, AUTH_JWT_PUBLIC_KEY, GH_APP_BOT_LOGIN"
    )
    assert output == ""


# --- the production bundle: staging's steps, run in Environment production ---------------------


def _env_entries(step: str) -> dict[str, str]:
    """The ``NAME: expression`` entries of the step's env block (comments left out)."""
    block = step.split("        env:\n", 1)[1].split("        run: |\n", 1)[0]
    return dict(re.findall(r"(?m)^          ([A-Z_0-9]+): (.+)$", block))


def test_prod_bundles_the_secrets_exactly_like_staging() -> None:
    prod, staging = _step(BUNDLE), _workflow_step(BUNDLE, CI_WORKFLOW)

    assert _run_script(prod) == _run_script(staging)
    assert _env_entries(prod) == _env_entries(staging)
    assert "        id: app_secrets\n        shell: bash\n" in prod


def test_prod_validates_the_bundle_like_staging_before_any_host_step() -> None:
    prod, staging = _step(VALIDATE), _workflow_step(VALIDATE, CI_WORKFLOW)

    assert _run_script(prod) == _run_script(staging)
    assert _env_entries(prod) == {
        "GHCR_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GHCR_USER": "${{ github.actor }}",
        "IMAGE": "${{ steps.digest.outputs.image }}",
        "APP_SECRETS_B64": "${{ steps.app_secrets.outputs.bundle }}",
        "REGISTRY": "ghcr.io",
    }
    assert "        shell: bash\n" in prod
    assert re.search(r"(?m)^        if:", prod) is None
    names = _step_names(_job("deploy-prod"))
    assert names.index(CHECKOUT) < names.index(BUNDLE) < names.index(VALIDATE) < names.index(UPLOAD)


def test_the_production_bundle_reaches_only_its_role_files(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.mkdir()
    result, output, _ = _run(BUNDLE, runner, CI_SECRETS)
    assert result.returncode == 0, result.stderr
    (line,) = output.splitlines()
    bundle = line.removeprefix("bundle=")
    assert bundle
    host = make_host(tmp_path, project=PROD_PROJECT)

    deploy = _deploy_step(host, IMAGE_A, "401-1", bundle=bundle)

    assert deploy.returncode == 0, deploy.stderr
    for file_name, (_, keys) in ENV_FILES.items():
        assert _read_env_file(host.app_dir / file_name) == {
            name: CI_SECRETS[name] for name in keys if CI_SECRETS[name]
        }, file_name
    # The store password is the one generated on the host, not a value from GitHub.
    assert re.fullmatch(r"[0-9a-f]{48}", _read_dotenv(host.env_file)["POSTGRES_PASSWORD"])
    _assert_prod_only(host, bundle=bundle)


# --- the documentation of Environment production ------------------------------------------------


def _production_section() -> str:
    """docs/SECRETS.md from the Environment production heading to the next ``### `` heading."""
    text = SECRETS_DOC.read_text(encoding="utf-8")
    head = "### GitHub Environment `production`"
    assert text.count(head) == 1, "SECRETS.md needs exactly one Environment production section"
    return re.split(r"(?m)^### ", text.split(head, 1)[1], maxsplit=1)[0]


def test_every_name_deploy_prod_reads_is_documented_for_production() -> None:
    job = _job("deploy-prod")
    names = set(re.findall(r"(?<!\w)(?:secrets|vars)\.(\w+)", job))
    assert {"GH_APP_PRIVATE_KEY", "GH_APP_BOT_LOGIN", "VPS_DMC268_U", "STAGING_HOST"} <= names
    section = _production_section()

    undocumented = sorted(name for name in names if f"`{name}`" not in section)

    assert undocumented == []


def test_every_production_secret_has_a_rotation_row() -> None:
    section = _production_section()
    # The secrets the job reads, less the ones the section names as inherited from the
    # organization or the repository: those are not rotated here.
    inherited = next(p for p in section.split("\n\n") if p.startswith("Наследуются"))
    secrets = set(re.findall(r"(?<!\w)secrets\.(\w+)", _job("deploy-prod")))
    owned = {name for name in secrets if f"`{name}`" not in inherited}
    assert owned >= {*REQUIRED_SECRETS, "GITLAB_CREDENTIAL_KEYS", "BITBUCKET_CREDENTIAL_KEYS"}
    rotation = section.split("#### Ротация", 1)[1].split("\n#### ", 1)[0]
    first_cells = [line.split("|")[1] for line in rotation.splitlines() if line.startswith("| `")]

    for name in sorted(owned):
        assert [cell for cell in first_cells if f"`{name}`" in cell], name


def test_every_rotation_row_says_what_restarts_and_what_breaks() -> None:
    rotation = _production_section().split("#### Ротация", 1)[1].split("\n#### ", 1)[0]
    rows = [line.split("|")[1:-1] for line in rotation.splitlines() if line.startswith("| `")]

    assert rows
    for name, _how, restarts, breaks in rows:
        assert restarts.strip(), f"no restart cell: {name}"
        assert breaks.strip(), f"no outage cell: {name}"


def test_the_production_section_forbids_copying_staging_values() -> None:
    section = _production_section()

    assert "значения staging не копировать" in section
    assert "`BITBUCKET_WEBHOOK_BASE_URL=https://api.<APP_DOMAIN>`" in section
    assert "на уровне repository или organization" in section


def test_the_production_section_holds_the_registration_procedure() -> None:
    section = _production_section()
    assert "в теле PR" not in section
    procedure = section.split("#### Регистрация и проверка", 1)[1].split("\n#### ", 1)[0]
    steps = re.findall(r"(?m)^\d+\. ", procedure)

    assert len(steps) == 5
    assert len([line for line in procedure.splitlines() if line.strip()]) <= 10
    assert "Prod webhook outcome" in procedure


def test_the_permissions_and_organization_tables_name_the_prod_workflows() -> None:
    text = SECRETS_DOC.read_text(encoding="utf-8")
    permissions = text.split("\n## 6. Permissions\n", 1)[1].split("\n## ", 1)[0]
    rows = {line.split("|")[1].strip(): line for line in permissions.splitlines() if "|" in line}

    assert "`contents: read`, `packages: read`" in rows["Deploy prod: `resolve`, `deploy-prod`"]
    for lookup in ("prod-webhook-outcome.yml", "staging-run-logs.yml"):
        assert rows[f"`{lookup}`: `lookup`"].split("|")[2].strip() == "`contents: read`"
    for name in ("VPS_DMC268_U", "VPS_DMC268_P"):
        (row,) = [line for line in text.splitlines() if line.startswith(f"| `{name}` |")]
        assert "prod-webhook-outcome.yml" in row, name
        assert "staging-run-logs.yml" in row, name


def test_the_lock_while_waiting_caveat_is_stated_once_and_pointed_to() -> None:
    # CICD.md section 5 item 6 holds the full statement; the other places only point to it.
    cicd = (REPO_ROOT / "docs" / "CICD.md").read_text(encoding="utf-8")
    secrets = SECRETS_DOC.read_text(encoding="utf-8")
    prod_section = _production_section()
    item_4 = secrets.split("Рекомендация для staging:", 1)[1].split("\n5. ", 1)[0]
    full = [line for line in cicd.splitlines() if "не проверено" in line and "approve" in line]

    assert len(full) == 1 and full[0].lstrip().startswith("- Job Deploy prod ждёт approve")
    for text in (prod_section, item_4, _text()):
        assert "не проверено" not in text and "unverified" not in text
    for text in (prod_section, item_4):
        assert "CICD.md" in text and "п. 6" in text
    assert "CICD.md" in _text()


def test_the_docs_say_a_jwt_key_change_keeps_users_logged_in() -> None:
    text = SECRETS_DOC.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `AUTH_JWT_PRIVATE_KEY` +"))

    assert "разлогинивает всех" not in text
    assert "проверить, что SPA" not in row
    assert "src/shared/api/client.ts" in row
    assert "один раз получит 401" in row
    assert "refresh-токен переживает смену" in text
