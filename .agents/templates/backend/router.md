# FastAPI router entrypoint

Requires: api #4 (layout, SQLAlchemy)

## When to use

A new HTTP endpoint. The router only decodes the transport contract (pydantic
request/response models, camelCase wire format) and delegates to a use case injected
via `Depends` — it never contains business logic or talks to a session directly.

## File placement

- `app/entrypoints/api/foo_router.py`
- `tests/test_foo_router.py`

## Code

<!-- proof: app/schemas.py -->

```python
from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CreateFooRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    display_name: str


class CreateFooResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    foo_id: str
    display_name: str
```

<!-- proof: app/router.py -->

```python
from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Depends

from .schemas import CreateFooRequest, CreateFooResponse


class CreateFooUseCase(Protocol):
    async def __call__(self, display_name: str) -> tuple[str, str]: ...


router = APIRouter(prefix="/foos", tags=["foos"])


def get_create_foo_use_case() -> CreateFooUseCase:
    raise NotImplementedError("wired by the application's dependency container")


@router.post("", response_model=CreateFooResponse, status_code=201)
async def create_foo(
    request: CreateFooRequest,
    use_case: Annotated[CreateFooUseCase, Depends(get_create_foo_use_case)],
) -> CreateFooResponse:
    foo_id, display_name = await use_case(request.display_name)
    return CreateFooResponse(foo_id=foo_id, display_name=display_name)
```

## Test

<!-- proof: tests/test_proof_router.py -->

```python
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.__proof__.router.router import CreateFooUseCase, get_create_foo_use_case, router


def test_create_foo_returns_camel_case_body() -> None:
    app = FastAPI()
    app.include_router(router)

    async def fake_use_case(display_name: str) -> tuple[str, str]:
        return "11111111-1111-4111-8111-000000000001", display_name

    fake: CreateFooUseCase = fake_use_case
    app.dependency_overrides[get_create_foo_use_case] = lambda: fake
    client = TestClient(app)

    response = client.post("/foos", json={"displayName": "widget"})

    assert response.status_code == 201
    assert response.json()["fooId"] == "11111111-1111-4111-8111-000000000001"
```

## Checklist

- `Annotated[X, Depends(...)]` on the parameter, never `Depends(...)` as a default
  value — a call in a default argument is a lint error (ruff `B008`).
- `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)` on every
  request/response model — the wire format is camelCase, the Python attribute is
  `snake_case` (pending role 6 decision; `rules/backend.md` carries the same caveat).
- The handler body is two lines: call the use case, build the response — no branching,
  no session, no repository import.
- The dependency is overridden with a fake async callable in the test, on a local
  `FastAPI()` app, never the real application instance.
- Production placement is `app/entrypoints/api/`; the marker file
  names above are proof-run paths only.
