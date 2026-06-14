# API contract

Mission Control exposes a FastAPI JSON API under `/api/v1`.
The OpenAPI document at `/openapi.json` is the canonical machine-readable
contract. This document records the human conventions that generated clients,
agents, and routers should follow during the clean-architecture migration.

## Contract Sources

- OpenAPI: `GET /openapi.json`
- FastAPI routers: `backend/app/presentation/api`
- Request/response schemas: `backend/app/presentation/schemas`
- Backward-compatible route shims: `backend/app/api`

Do not hand-edit generated frontend clients under
`frontend/src/api/generated/`; regenerate them from OpenAPI.

## Naming

Current API/domain language uses Project and Workspace. Legacy board routes may
still exist where agents or generated clients depend on them.

| Current concept | Legacy alias |
| --- | --- |
| Project | Board |
| Workspace | BoardGroup |
| project_id | board_id |
| workspace_id | board_group_id |
| is_project_lead | is_board_lead |

New request/response fields should use current names. Compatibility schemas may
accept legacy names with explicit coercion.

## Authentication Headers

Human/user APIs use:

```http
Authorization: Bearer <user-token>
```

Agent APIs use:

```http
X-Agent-Token: <agent-token>
```

Shared user-or-agent routes may accept an agent token as a bearer token only
after user authentication does not resolve. Agent tokens are rate-limited and
stored only as hashes.

## Common Response Shapes

Single-resource endpoints return a schema object such as `TaskRead`,
`AgentRead`, `ApprovalRead`, or `ProjectRead`.

List endpoints should use limit/offset pagination unless there is a strong
reason not to:

```json
{
  "items": [],
  "total": 0,
  "limit": 50,
  "offset": 0
}
```

Mutation endpoints should return the updated read model when the caller needs
the new state. Simple command endpoints may return:

```json
{"ok": true}
```

## Task Contract

Task create/update payloads support these core fields:

- `title`
- `description`
- `status`
- `status_reason`
- `output`
- `priority`
- `due_at`
- `assigned_agent_id`
- `depends_on_task_ids`
- `tag_ids`
- `custom_field_values`
- `comment` on update paths that support inline comments

A task cannot be marked `done` unless:

- `output` is filled with non-blank text.
- All custom fields marked `required_for_done` are filled.
- Project review/approval/dependency gates allow the transition.

Task read models expose `change_log`, an append-only JSON summary of task
create/update activity. The activity timeline remains the audit source of truth;
`change_log` is a convenient task-local summary.

## Error Shape

Errors should preserve FastAPI-compatible `detail` and include the request id
when middleware is able to attach it:

```json
{
  "detail": "Permission denied",
  "request_id": "req_..."
}
```

Common status codes:

| Status | Meaning |
| --- | --- |
| 400 | malformed semantic input |
| 401 | missing or invalid credentials |
| 403 | authenticated actor lacks permission |
| 404 | resource missing or not visible |
| 409 | conflict with current lifecycle/business state |
| 413 | payload too large |
| 422 | request validation failed |
| 429 | rate limit exceeded |
| 500 | unhandled server error |
| 502 | gateway/upstream call failed |

Domain errors should be mapped through presentation error mapping instead of
raised as raw infrastructure exceptions.

## Agent Routes

Agent-facing routes are designed for weaker models as well as capable models.
OpenAPI metadata should include role/intention hints where practical:

- `tags`: `agent-main`, `agent-lead`, or `agent-worker`
- `x-llm-intent`
- `x-when-to-use`
- `x-when-not-to-use`
- `x-routing-policy`
- `x-prerequisites`
- `x-side-effects`

Token-bearing instructions are rendered into workspace files such as `TOOLS.md`
and `HEARTBEAT.md`. Resend-token must refresh every rendered file that can
contain `AUTH_TOKEN`, not only the visible tools file.

Gateway-main agent access is organization-scoped through the gateway attached to
the authenticated token. A null `project_id` means "main agent for this gateway",
not global access across every organization.

## Server-Sent Events

SSE endpoints should emit stable event names and JSON payloads. Existing
patterns:

- Agent stream emits `agent` events with serialized agent state.
- Approval stream emits `approval` events with approval state and task count
  context.
- Project memory stream emits `memory` events with serialized memory state.

SSE payload generation belongs in application services when it requires business
state, not directly in routers.

## Compatibility Policy During Refactor

Compatibility shims are allowed while frontend/generated clients and tests move
to the new names. Shims should:

- Re-export new modules.
- Coerce old field names to new field names when needed.
- Avoid owning new business logic.
- Be listed for removal in the final cleanup phase.

## Change Checklist

When adding or changing an endpoint:

1. Add or update the presentation schema.
2. Implement orchestration in an application use case.
3. Keep router code limited to dependency parsing and service delegation.
4. Add OpenAPI metadata useful to humans, generated clients, and agents.
5. Update generated frontend client after backend OpenAPI changes.
6. Update docs if the contract changes.
