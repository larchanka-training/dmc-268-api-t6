"""Compose headless GitHub authorization with short database transactions."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from app.bootstrap.auth_api import CallbackInstallationLinker
from app.bootstrap.portal_auth import get_agent_tokens
from app.bootstrap.reviews_api import GitHubAuthHttpClients, ReviewsApiResources
from app.modules.auth.application.agent_device_access import AgentDeviceAccess
from app.modules.auth.infrastructure.agent_device_flows import SqlAlchemyDeviceFlowUnitOfWork
from app.modules.auth.infrastructure.github_oauth import (
    HttpGitHubDeviceOAuthClient,
    HttpGitHubUserProfile,
)


def get_agent_device_access(request: Request) -> AgentDeviceAccess:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    clients = getattr(request.app.state, "github_auth_http_clients", None)
    client_id = os.environ.get("GITHUB_CLIENT_ID")
    if (
        not isinstance(resources, ReviewsApiResources)
        or not isinstance(clients, GitHubAuthHttpClients)
        or not client_id
    ):
        raise HTTPException(status_code=503, detail="GitHub device authorization is not configured")
    return AgentDeviceAccess(
        github=HttpGitHubDeviceOAuthClient(clients.oauth, client_id=client_id),
        profile=HttpGitHubUserProfile(clients.api),
        linker=CallbackInstallationLinker(resources.github_installation_linker(client=clients.api)),
        tokens=get_agent_tokens(request),
        uow_factory=lambda: SqlAlchemyDeviceFlowUnitOfWork(resources.session_factory),
    )
