# Portal authorization handoff for #34

Existing `/api/runs` routes use `get_auth_scope` for a fixed RS256 Bearer check and
`get_run_repository` for database-scoped reads and cancellation. `/api/auth/me` uses the
same Bearer scope. OAuth callback, refresh, and logout retain their separate auth methods.

When #34 adds repository or pull request routes, inject `AuthScope` through
`app.bootstrap.portal_auth.get_auth_scope`. Apply
`app.modules.workspaces.infrastructure.repository_access.repository_access_predicate(scope)`
to the `Repository` query **before** its limit, cursor, detail read, or mutation. The
predicate intersects claimed Workspace IDs with current
`GitHubUserWorkspaceAccess` and `GitHubUserRepositoryAccess` rows, matching both the
installation ID and repository external ID. A Workspace grant alone cannot authorize
all repositories under an installation. Return 404 for a detail or mutation outside
this scope. For streams, resolve each emitted resource through a scoped query so
revoked grants stop subsequent events.

`ReviewsApiResources.run_repository()` remains unscoped for trusted worker code.
Portal handlers must use the scoped FastAPI dependency, `get_run_repository`, or
pass an `AuthScope` explicitly when composing a repository.
