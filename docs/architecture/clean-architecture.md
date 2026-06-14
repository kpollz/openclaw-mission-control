# Backend clean architecture

Mission Control backend is being refactored toward a layered FastAPI service.
The goal is to make runtime behavior live in application/domain code, while API
routers stay thin and database/gateway details stay behind infrastructure
modules.

## Layer Map

```text
backend/app/
├── domain/            Pure rules, entities, repository interfaces, domain errors
├── application/       Use cases, orchestration services, DTOs, ports
├── infrastructure/    SQLModel models, database, auth, gateway, queue, webhooks
├── presentation/      FastAPI routers, request/response schemas, error mapping
├── shared/            Cross-cutting helpers with no business ownership
└── api/core/db/...    Temporary backward-compatible shims
```

## Dependency Rule

Dependencies should point inward:

```text
presentation -> application -> domain
application  -> infrastructure only for current migration-era persistence/gateway calls
domain       -> standard library only, plus domain-local modules
infrastructure -> domain/shared allowed, presentation not allowed
```

During the migration, some application services still call SQLModel query
helpers directly. That is accepted only as an intermediate step; new shared
behavior should prefer repository/port boundaries where practical.
Application services should not import FastAPI routers or presentation
dependencies; shared actor/request context belongs in application DTOs.

## Layer Responsibilities

### Domain

Domain code contains rules that can be reasoned about without FastAPI,
SQLAlchemy, gateway clients, or environment configuration.

Current examples:

- `domain/services/task_lifecycle.py`: task status transition and done-gate rules.
- `domain/services/task_permission.py`: task field-level permission rules.
- `domain/services/task_dependencies.py`: dependency graph validation.
- `domain/services/agent_policy.py`: agent/project/gateway authorization rules.
- `domain/services/agent_presence.py`: heartbeat/presence status normalization.
- `domain/exceptions.py`: typed domain errors.

### Application

Application use cases own orchestration. They are the place for transaction
flow, loading related models, calling domain rules, dispatching notifications,
and shaping response models.

Current use case facades:

- `application/dtos/common.py` for cross-use-case DTOs such as `ActorContext`
- `application/use_cases/tasks/service.py`
- `application/use_cases/projects/service.py`
- `application/use_cases/organizations/service.py`
- `application/use_cases/tags/service.py`
- `application/use_cases/gateways/service.py`
- `application/use_cases/workspaces/service.py`
- `application/use_cases/approvals/service.py`
- `application/use_cases/agents/provisioning_db.py` plus agent submodules
- `application/use_cases/agents/tokens.py`
- `application/use_cases/agents/heartbeat.py`
- `application/use_cases/agents/project_context.py`
- `application/use_cases/project_memory/service.py`

`agents/provisioning_db.py` is still the largest migration target. It currently
combines listing, heartbeat, create/update/delete, and provisioning coordination.
Token resend/rotation has been extracted to `agents/tokens.py`, and heartbeat
presence persistence has been extracted to `agents/heartbeat.py`. Agent-facing
read-only project context has been extracted to `agents/project_context.py`. The
project-context service also owns organization-scoped visibility for gateway-main
agents. Project memory listing, stream event fetch/serialization, chat writes,
mentions, and chat notifications live in `project_memory/service.py`. The
intended direction is to keep splitting large agent workflows into smaller
application services while preserving the thin router contract.

### Infrastructure

Infrastructure owns external details:

- `infrastructure/models`: SQLModel table models.
- `infrastructure/database`: engine/session/query helpers/pagination.
- `infrastructure/persistence`: repository implementations and DB helper modules.
- `infrastructure/gateway`: OpenClaw gateway RPC/provisioning/session helpers.
- `infrastructure/auth`: user and agent authentication internals.
- `infrastructure/queue`: background queue implementations.
- `infrastructure/webhooks`: webhook queue/dispatch.

Infrastructure modules may translate external errors into application-visible
errors, but business decisions should not live here unless they are purely about
that integration.

### Presentation

Presentation owns HTTP concerns:

- FastAPI route registration.
- Dependency injection.
- Request query/body parsing.
- Response model declarations.
- HTTP error mapping.

Routers should delegate to application services and avoid direct business
orchestration. Thin-router examples are `presentation/api/tasks.py`,
`presentation/api/projects.py`, `presentation/api/tags.py`,
`presentation/api/gateways.py`, and `presentation/api/approvals.py`.

## Naming Migration

The domain language is moving from:

| Legacy | Current |
| --- | --- |
| Board | Project |
| BoardGroup | Workspace |
| board_id | project_id |
| board_group_id | workspace_id |
| is_board_lead | is_project_lead |

Database table names and new code use the current names. Temporary shims remain
under old import paths so older tests, generated clients, and external callers
can be migrated incrementally.

## Backward-Compatible Shims

The following directories are compatibility layers, not the desired long-term
architecture:

- `backend/app/api`
- `backend/app/core`
- `backend/app/db`
- `backend/app/models`
- `backend/app/schemas`
- `backend/app/services`

They should only re-export or adapt to the new layer. Do not add new business
logic to shim modules.

## Agent Lifecycle Notes

Agent lifecycle is intentionally stricter than ordinary CRUD because it touches
gateway workspaces and token-bearing files.

Important invariants:

- Agent tokens are opaque and stored only as hashes.
- Gateway-main agents are scoped through their gateway organization; they must
  not gain cross-organization project or agent visibility just because their
  `project_id` is null.
- Resend-token rotates the backend hash and writes token-bearing workspace files
  (`TOOLS.md`, `HEARTBEAT.md`, and related rendered files) before committing the
  new state as successful.
- If gateway file writes fail, resend-token restores the previous token hash.
- `application/use_cases/agents/tokens.py` is the owner of resend-token behavior.
- Successful agent heartbeat or authenticated agent activity should move
  `provisioning`/`updating` agents to `online`.
- `application/use_cases/agents/heartbeat.py` is the owner of persisted heartbeat
  state and computed agent read status.
- `deleting` is protected and must not be converted to `online` by heartbeat.

## Task Lifecycle Notes

Task lifecycle is centered in `application/use_cases/tasks/service.py` with pure
permission/status helpers in `domain/services`.

Important invariants:

- `tasks.output` is the first-class deliverable field.
- A task cannot enter `done` with blank `output`.
- Project-specific custom fields can add more `required_for_done` gates.
- `change_log` stores task-local create/update snapshots; activity events remain
  the cross-resource audit trail.
- Agent update permissions allow workers to set `status` and `output` only on
  tasks they are allowed to operate.

## Refactor Rules For New Work

- Add pure business decisions to `domain/services` first when they do not need
  IO.
- Add user/system workflows to `application/use_cases`.
- Keep FastAPI routers thin.
- Keep SQLModel models in `infrastructure/models`; expose legacy imports through
  shims only when needed.
- Prefer explicit compatibility adapters over mixing legacy and current names in
  new core logic.
- Update this document when a boundary changes.
