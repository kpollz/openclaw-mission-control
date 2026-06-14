# Clean Architecture Refactoring — Progress Tracker

**Last updated:** 2026-06-13 (ALL PHASES COMPLETE)
**Status:** ✅ All 8 phases done. Board→Project hard rename complete. All shim directories removed. **478 tests pass, 1 xfailed. App loads 151 routes.**

---

## Summary

The entire Clean Architecture refactoring is complete:

- **Domain → Application → Infrastructure → Presentation** layer separation
- **Board→Project**, **BoardGroup→Workspace** hard rename (no backward-compat shims remain)
- **All old import paths** (`app/core/`, `app/db/`, `app/models/`, `app/schemas/`, `app/api/`, `app/services/`) **deleted**
- **478/478 tests passing**, 1 xfailed
- **151 routes** loading cleanly

---

## Phase Completion

| Phase | Name | Status |
|-------|------|--------|
| 1 | Structural Reorganization | ✅ |
| 2 | Domain Layer | ✅ |
| 3 | Domain Services | ✅ |
| 4 | Naming Rename (Board→Project, BoardGroup→Workspace) | ✅ |
| 5 | Auth Overhaul (Register/Login + Ownership Scoping) | ✅ |
| 6 | Build Use Cases — Projects & Tasks | ✅ |
| 7 | Build Use Cases — Remaining Aggregates | ✅ |
| 8 | Documentation | ✅ |
| Final | Test Alignment + Shim Removal | ✅ |

---

## What's Done

### Phase 1: Structural Reorganization ✅
- All files moved to new directories (domain/, application/, infrastructure/, presentation/, shared/)
- Re-export shims created at all old locations (now deleted)

### Phase 2: Domain Layer ✅
- `domain/exceptions.py` — DomainError hierarchy
- `domain/entities/` — 12 pure dataclass entities with `from_model()`/`apply_to_model()`
- `domain/repositories/` — AbstractRepository base + 10 interfaces
- `presentation/error_mapper.py` — maps DomainError → HTTPException
- Repository implementations (5): Task, Agent, Project, Gateway + base

### Phase 3: Domain Services ✅
- `domain/services/task_lifecycle.py` — TaskLifecycleRules, CustomFieldRules
- `domain/services/task_permission.py` — TaskPermissionRules
- `domain/services/task_dependencies.py` — pure graph functions + async DB via TaskDependencyRepository
- `domain/services/agent_policy.py` — DomainError-based, no infrastructure imports
- `domain/services/lead_policy.py` — pure
- `domain/services/mention.py` — pure
- `domain/services/agent_presence.py` — heartbeat/presence status normalization

### Phase 4: Naming Rename ✅
- Board→Project, BoardGroup→Workspace across all layers
- DB tables renamed via Alembic migration `b0a1d2e3f4a5`
- All schemas, models, routes, templates, error messages updated

### Phase 5: Auth Overhaul ✅
- AuthMode.PASSWORD added (local, clerk, password)
- Register/Login/Refresh/Change Password endpoints
- Password auth with PBKDF2-SHA256 hashing
- JWT access/refresh tokens
- Ownership scoping with `created_by` FK on 6 business models

### Phase 6: Build Use Cases — Projects & Tasks ✅
- `TaskService` facade (~1300 lines) — all task business logic
- `ProjectService` facade (~280 lines) — project update + notifications
- TaskDependencyRepository — all async DB functions
- Thin routers: tasks.py 3027→182 lines, projects.py 610→165 lines

### Phase 7: Build Use Cases — Remaining Aggregates ✅
- **Organizations** — OrganizationService facade
- **Tags** — TagService facade
- **Gateways** — GatewayService facade
- **Workspaces** — WorkspaceService facade
- **Approvals** — ApprovalService facade
- **Agents** — extracted tokens, heartbeat, project_context, project_memory sub-services
- **Activity** — ActivityService facade
- **Metrics** — MetricsService facade
- **Skills Marketplace** — SkillsMarketplaceService facade
- **Users** — UserService facade
- **Auth** — AuthService facade
- **Onboarding** — ProjectOnboardingService facade
- **Webhooks** — WebhookService facade
- **Souls Directory** — standalone service module

### Phase 8: Documentation ✅
- `docs/architecture/clean-architecture.md`
- `docs/reference/api-contract.md`
- `docs/reference/schema.md`
- `README.md` updated with architecture section

### Task Model Expansion ✅
- `Task.output` and `Task.change_log` fields
- Migration `a7c9d1e2f3b4`
- Non-blank `output` enforced before `done` status
- Agent templates updated

### Final: Test Alignment ✅
- All 478 tests aligned to new architecture
- Import paths updated: `app.models.*` → `app.infrastructure.models.*`, `app.schemas.*` → `app.presentation.schemas.*`, etc.
- Monkeypatch targets updated to service modules
- DomainError assertions where applicable

### Final: Shim Removal ✅
- Deleted: `app/core/`, `app/db/`, `app/models/`, `app/schemas/`, `app/api/`, `app/services/`
- All imports in app code, tests, and scripts updated to canonical paths
- `app/infrastructure/database/engine.py` updated to import models from `app.infrastructure.models`
- Module-level test aliases preserved in canonical routers (`tasks.py`, `activity.py`)

---

## Final Directory Structure

```
backend/app/
├── main.py
├── domain/
│   ├── entities/          # 12 entities (ProjectEntity, TaskEntity, AgentEntity, etc.)
│   ├── repositories/      # 10 abstract interfaces + base
│   ├── services/          # 7 domain services (pure business rules)
│   └── exceptions.py      # DomainError hierarchy
├── application/
│   ├── use_cases/         # Service facades per aggregate
│   │   ├── tasks/         # TaskService
│   │   ├── projects/      # ProjectService
│   │   ├── agents/        # provisioning_db, tokens, heartbeat, project_context, etc.
│   │   ├── organizations/
│   │   ├── approvals/
│   │   ├── gateways/
│   │   ├── tags/
│   │   ├── workspaces/
│   │   ├── activity/
│   │   ├── metrics/
│   │   ├── skills/
│   │   ├── users/
│   │   ├── auth/
│   │   ├── onboarding/
│   │   ├── webhooks/
│   │   ├── project_memory/
│   │   ├── workspace_memory/
│   │   ├── task_custom_fields/
│   │   └── souls_directory.py
│   ├── dtos/              # DTOs (common, task, project, agent)
│   └── ports/             # External service interfaces
├── infrastructure/
│   ├── persistence/       # Repository implementations (6)
│   ├── database/          # engine, session, crud, queryset, query_manager, pagination
│   ├── models/            # 28 SQLModel ORM models
│   ├── gateway/           # rpc_client, resolver, dispatch, provisioner, etc.
│   ├── auth/              # clerk_local_auth, agent_auth, agent_tokens, password_auth, jwt_service
│   ├── notifications/     # activity_recorder
│   ├── queue/             # redis_queue, worker, lifecycle_queue
│   └── webhooks/          # dispatch, queue
├── presentation/
│   ├── api/               # 24 thin router files
│   ├── schemas/           # 27 schema files
│   ├── deps.py
│   ├── error_handling.py
│   └── error_mapper.py
└── shared/
    ├── config.py
    ├── logging.py
    ├── time.py
    ├── auth_mode.py
    ├── rate_limit.py
    ├── rate_limit_backend.py
    ├── security_headers.py
    ├── client_ip.py
    ├── version.py
    └── durations.py
```
