"""Trusted local operator CLI; secret input never travels in arguments or output."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Never
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.modules.integrations.gitlab.application.manage_connection import (
    ConnectionResult,
    GitLabConnectionLifecycle,
    GitLabManagementError,
    ManageGitLabConnection,
)
from app.modules.integrations.gitlab.infrastructure.connection_store import (
    CredentialCipherAdapter,
    SqlAlchemyGitLabManagementUnitOfWork,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails
from app.modules.integrations.gitlab.infrastructure.settings import (
    DatabaseSettings,
    GitLabRuntimeSettings,
    GitLabSettingsError,
    strict_json,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(2, "arguments_invalid\n")


def _positive(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 9223372036854775807:
        raise ValueError
    return result


def _descriptor(value: str) -> int:
    result = int(value)
    if not 0 <= result <= 2147483647:
        raise ValueError
    return result


def _parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(
        prog="python -m app.gitlab_admin",
        description="Manage GitLab connections; create and bind leave intake disabled.",
        epilog=(
            "Secrets: --secrets-fd N (default 0) reads bounded UTF-8 JSON from a pipe or "
            "already-open protected file descriptor; echoed TTY input is refused. "
            "create requires token and webhook_secret; rotate-token requires token. "
            "Provision that file with owner-only access, or use a secret-manager pipe. "
            "Container example (run on the host, using the staging Compose file/environment "
            "selection in the runbook): docker compose --profile gitlab-operator run --rm -T "
            "gitlab-admin "
            "create [public options] --secrets-fd 0 < /protected/secrets.json. "
            "This redirects the protected host file to container stdin (FD 0). "
            "Local process only: python -m app.gitlab_admin create [public options] "
            "--secrets-fd 3 3< /protected/secrets.json. "
            "Compose does not forward extra descriptors such as FD 3. "
            "Config: DATABASE_URL, GITLAB_CREDENTIAL_KEYS (JSON key-ID/base64 object), "
            "GITLAB_ACTIVE_KEY_ID, GITLAB_ALLOWED_ORIGINS (JSON array), optional "
            "GITLAB_PRIVATE_ORIGINS (JSON array). SSL_CERT_FILE optionally supplies a PEM "
            "CA bundle with TLS verification. inspect/revoke need only DATABASE_URL. "
            "For prompt_not_seeded, inside the image: python -m app.bootstrap.seed_prompts. "
            "For rule_assets_invalid: restore the packaged review/rules assets. "
            "activate reauthenticates the stored credential and requires ready bindings, rules "
            "and prompt. Deployment activation requires the external #171 merged/rebased gate. "
            "No token environment fallback."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "create",
        "bind-project",
        "rotate-token",
        "activate",
        "revoke-installation",
        "revoke-workspace",
        "inspect",
    ):
        command = commands.add_parser(name, allow_abbrev=False)
        if name in ("create", "revoke-workspace"):
            command.add_argument("--workspace-id", required=True, type=UUID)
        if name != "revoke-workspace":
            command.add_argument("--installation-id", required=True, type=UUID)
        if name == "create":
            command.add_argument("--origin", required=True)
        if name == "bind-project":
            command.add_argument("--project-id", required=True, type=_positive)
        if name in ("rotate-token", "activate"):
            command.add_argument(
                "--expected-generation", type=_positive, required=name == "activate"
            )
        if name in ("create", "rotate-token"):
            command.add_argument("--secrets-fd", type=_descriptor, default=0)
    return parser


class SecretInputError(ValueError):
    def __init__(self) -> None:
        super().__init__("secrets_invalid")


def read_secrets(fd: int, *, create: bool) -> dict[str, str]:
    """Read at most 8193 bytes from an inherited regular file or pipe; never echo."""
    try:
        mode = os.fstat(fd).st_mode
        if os.isatty(fd) or not (stat.S_ISREG(mode) or stat.S_ISFIFO(mode)):
            raise ValueError
        raw = bytearray()
        while len(raw) <= 8192:
            chunk = os.read(fd, min(4096, 8193 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        values = strict_json(bytes(raw), limit=8192)
        required = {"token", "webhook_secret"} if create else {"token"}
        if not isinstance(values, dict) or set(values) != required:
            raise ValueError
        result: dict[str, str] = {}
        for name in required:
            value = values[name]
            minimum, maximum = (32, 256) if name == "webhook_secret" else (1, 4096)
            if (
                not isinstance(value, str)
                or not minimum <= len(value) <= maximum
                or any(not 33 <= ord(c) <= 126 for c in value)
            ):
                raise ValueError
            result[name] = value
        return result
    except (OSError, ValueError, OverflowError):
        raise SecretInputError() from None


def connection_json(result: ConnectionResult) -> dict[str, object]:
    """Explicit public DTO serializer; credential envelopes never enter this boundary."""
    return {
        "installation_id": str(result.installation_id),
        "workspace_id": str(result.workspace_id),
        "instance_origin": result.instance_origin,
        "generation": result.generation,
        "intake_enabled": result.intake_enabled,
        "revoked": result.revoked,
    }


async def execute(
    args: argparse.Namespace, secrets: Mapping[str, str], env: Mapping[str, str]
) -> dict[str, object]:
    recovery = args.command in ("inspect", "revoke-installation", "revoke-workspace")
    settings = None if recovery else GitLabRuntimeSettings.from_environment(env)
    database = DatabaseSettings.from_environment(env) if settings is None else settings.database
    engine = create_async_engine(database.database_url, hide_parameters=True, echo=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if settings is not None:
            return await _connected_operation(args, secrets, settings, sessions)
        lifecycle = GitLabConnectionLifecycle(
            uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions)
        )
        if args.command == "revoke-installation":
            return connection_json(
                await lifecycle.revoke_installation(installation_id=args.installation_id)
            )
        if args.command == "revoke-workspace":
            results = await lifecycle.revoke_workspace(workspace_id=args.workspace_id)
            return {"connections": [connection_json(result) for result in results]}
        inspected = await lifecycle.inspect(installation_id=args.installation_id)
        return {
            "connection": connection_json(inspected.connection),
            "prompt_ready": inspected.prompt_ready,
            "repositories": [
                {
                    "repository_id": str(binding.repository_id),
                    "project_id": binding.project_id,
                    "enabled": binding.enabled,
                    "available": binding.available,
                    "rules_ready": binding.rules_ready,
                }
                for binding in inspected.repositories
            ],
        }
    finally:
        await engine.dispose()


async def _connected_operation(
    args: argparse.Namespace,
    secrets: Mapping[str, str],
    settings: GitLabRuntimeSettings,
    sessions: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    codec = CredentialCipherAdapter(
        CredentialCipher(active_key_id=settings.active_key_id, keys=settings.keys)
    )
    try:
        tls = ssl.create_default_context(cafile=settings.ssl_cert_file)
    except (OSError, ValueError):
        raise GitLabSettingsError() from None
    async with httpx.AsyncClient(
        verify=tls, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10.0)
    ) as client:
        projects = GitLabProjectDetails(client, origin_policy=settings.origin_policy)
        if args.command in ("rotate-token", "activate"):
            lifecycle = GitLabConnectionLifecycle(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
                codec=codec,
                projects=projects,
                origin_policy=settings.origin_policy,
            )
            if args.command == "activate":
                return connection_json(
                    await lifecycle.activate(
                        installation_id=args.installation_id,
                        expected_generation=args.expected_generation,
                    )
                )
            return connection_json(
                await lifecycle.rotate_token(
                    installation_id=args.installation_id,
                    token=secrets["token"],
                    expected_generation=args.expected_generation,
                )
            )
        try:
            rules = (
                load_default_rule_sets(Path(__file__).resolve().parents[1] / "review" / "rules")
                if args.command == "bind-project"
                else {}
            )
        except (ValueError, RecursionError):
            # Only asset loading is in this try: loader validation, UTF-8 decoding,
            # JSON integer limits and excessive nesting are invalid configuration.
            raise GitLabManagementError("rule_assets_invalid") from None
        management = ManageGitLabConnection(
            uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
            codec=codec,
            projects=projects,
            origin_policy=settings.origin_policy,
            rule_sets=rules,
        )
        if args.command == "create":
            return connection_json(
                await management.create(
                    installation_id=args.installation_id,
                    workspace_id=args.workspace_id,
                    instance_origin=args.origin,
                    token=secrets["token"],
                    webhook_secret=secrets["webhook_secret"],
                )
            )
        binding = await management.bind_project(
            installation_id=args.installation_id, project_id=args.project_id
        )
        return {
            "repository_id": str(binding.repository_id),
            "created": binding.created,
            "rules_created": binding.rules_created,
        }


_SAFE_MANAGEMENT_CODES = frozenset(
    {
        "activation_not_configured",
        "activation_no_bindings",
        "activation_not_ready",
        "connection_changed",
        "connection_not_found",
        "connection_revoked",
        "binding_failed",
        "credential_operation_failed",
        "installation_identity_conflict",
        "invalid_identity",
        "invalid_languages",
        "invalid_origin",
        "invalid_project",
        "invalid_token",
        "invalid_webhook_secret",
        "origin_not_allowed",
        "private_origin_not_allowed",
        "project_access_failed",
        "project_identity_mismatch",
        "project_reserved",
        "project_unavailable",
        "prompt_not_seeded",
        "rotation_not_configured",
        "rule_assets_invalid",
        "rule_assets_missing",
        "workspace_not_found",
    }
)


def main() -> int:
    args = _parser().parse_args()
    try:
        secrets = (
            read_secrets(args.secrets_fd, create=args.command == "create")
            if args.command in ("create", "rotate-token")
            else {}
        )
        result = asyncio.run(execute(args, secrets, os.environ))
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        return 0
    except SecretInputError:
        code = "secrets_invalid"
    except GitLabSettingsError:
        code = "configuration_invalid"
    except GitLabManagementError as exc:
        code = exc.code if exc.code in _SAFE_MANAGEMENT_CODES else "operation_failed"
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130
    except Exception:
        # The process is the final confidentiality boundary, including DB/HTTP/TLS failures.
        code = "operation_failed"
    sys.stderr.write(code + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
