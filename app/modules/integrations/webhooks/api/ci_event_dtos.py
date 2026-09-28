"""Parse actionable GitHub CI webhooks at the inbound transport boundary."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent

_SHA_PATTERN = r"^[0-9a-fA-F]{40}$"


class _Id(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)


class _Head(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    head_sha: str = Field(pattern=_SHA_PATTERN)


class _Common(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    installation: _Id
    repository: _Id


class _CheckSuiteDelivery(_Common):
    check_suite: _Head


class _WorkflowRunDelivery(_Common):
    workflow_run: _Head


class _StatusDelivery(_Common):
    sha: str = Field(pattern=_SHA_PATTERN)


def parse_ci_event(event_name: str, payload: Mapping[str, object]) -> CiTriggerEvent:
    if event_name == "check_suite":
        parsed = _CheckSuiteDelivery.model_validate(payload)
        sha = parsed.check_suite.head_sha
        installation_id = parsed.installation.id
        repository_id = parsed.repository.id
    elif event_name == "workflow_run":
        parsed_workflow = _WorkflowRunDelivery.model_validate(payload)
        sha = parsed_workflow.workflow_run.head_sha
        installation_id = parsed_workflow.installation.id
        repository_id = parsed_workflow.repository.id
    elif event_name == "status":
        parsed_status = _StatusDelivery.model_validate(payload)
        sha = parsed_status.sha
        installation_id = parsed_status.installation.id
        repository_id = parsed_status.repository.id
    else:
        raise ValueError("unsupported GitHub CI event")
    return CiTriggerEvent(installation_id, repository_id, sha)
