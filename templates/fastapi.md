## Structure

```
app/
├── main.py              # App init, router registration, lifespan
├── config.py            # Settings via pydantic-settings BaseSettings
├── database.py          # SQLAlchemy async engine + session factory
├── core/
│   ├── security.py      # JWT creation/validation, password hashing
│   ├── responses.py     # StandardResponse[T], PaginatedResponse[T]
│   └── exceptions.py    # Custom HTTP exception handlers (optional)
├── dependencies/
│   ├── auth.py          # get_current_user DI → injects current user
│   └── db.py            # get_db DI → yields AsyncSession
├── api/
│   └── v{N}/            # Current version — thin route handlers only
├── models/              # SQLAlchemy ORM models (domain-level tables)
├── schemas/             # Pydantic v2 DTOs
│   └── common.py        # Shared types, pagination
├── repo/                # DB query functions — no business logic
│   └── base.py          # BaseRepo: paginate helper
└── services/            # Business logic — orchestrates repo calls
```

## Layer contract

```
api/v{N}/  →  services/  →  repo/  →  models/
```

- **Routes** (`api/v{N}/`): auth, input validation, call one service function, return response envelope. Nothing else.
- **Services** (`services/`): business logic, orchestration across repos. No HTTPException — raise domain errors or return None and let the route decide.
- **Repo** (`repo/`): raw DB queries only. Accepts/returns ORM models or scalars. No business logic.
- **Models** (`models/`): SQLAlchemy ORM definitions. No logic.

Routes never import from `repo/` directly. Services never import from `api/`.

## Response envelopes

All endpoints return one of:

```python
StandardResponse[T]  →  { "success": True, "data": T }
PaginatedResponse[T] →  { "success": True, "data": list[T], "total": int, "page": int, "page_size": int }
```

```python
# app/core/responses.py
from pydantic import BaseModel
from typing import Generic, TypeVar

T = TypeVar("T")

class StandardResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T

class PaginatedResponse(BaseModel, Generic[T]):
    success: bool = True
    data: list[T]
    total: int
    page: int
    page_size: int
```

Exception: raw endpoints (e.g. `/health`, specific exports) may return unwrapped — document the exception.

## Versioning

- Active version: `app/api/v{N}/` where N is the current major version
- Register router in `main.py`: `app.include_router(v1_router, prefix="/api/v1")`
- New breaking version = new directory `v{N+1}/`; old version stays until clients migrate
- Non-breaking changes go into the current version

## Schemas (Pydantic v2)

- Input DTO: `XxxIn` (or `XxxCreate` / `XxxUpdate`)
- Output DTO: `XxxOut`
- All `XxxOut` models: `model_config = ConfigDict(from_attributes=True)`
- Never expose ORM model objects directly to routes — always map through XxxOut

## Models (SQLAlchemy 2.0 async)

- UUID primary key on every table:
  ```python
  id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
  ```
- `created_at` / `updated_at` on every table (use `server_default` / `onupdate`)
- Use `Mapped[T]` type annotations — not legacy `Column()` style
- Relationships declared on both sides with `back_populates`

## Auth

- JWT stored in httpOnly cookie (not Authorization header)
- `get_current_user` dependency injected at router or individual route level
- Secrets always from `config.py` (pydantic-settings + .env) — never hardcoded

## Migrations (Alembic)

- One migration per logical change
- Generate: `alembic revision --autogenerate -m "description"`
- Always review generated file before running — autogenerate misses some things (e.g. enum changes)
- Apply: `alembic upgrade head`
- Rollback: `alembic downgrade -1`

## Error handling

- Routes raise `HTTPException` — services return `None` for not-found; route decides whether to 404
- Standard codes: 404 not found, 422 validation (automatic from Pydantic), 401 unauthenticated, 403 forbidden
- Do not swallow exceptions in services — let them propagate to the global exception handler

## Add feature workflow

1. `models/` → ORM model + Alembic migration
2. `schemas/` → XxxIn / XxxOut
3. `repo/` → DB query functions
4. `services/` → business logic (skip if trivial CRUD)
5. `api/v{N}/` → thin route handler
6. Register router in `main.py`
7. `alembic upgrade head`
8. If frontend exists: regenerate OpenAPI client types
