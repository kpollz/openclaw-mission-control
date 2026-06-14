# Database schema map

Mission Control stores runtime state in PostgreSQL in normal Docker and
production deployments. Some tests use temporary SQLite databases for fast,
isolated checks, but PostgreSQL is the deployment target.

SQLModel table definitions live in `backend/app/infrastructure/models`.
Legacy imports under `backend/app/models` are compatibility shims.

## Core Tenancy

| Table | Model | Purpose |
| --- | --- | --- |
| `users` | `User` | Human actors across auth modes. |
| `organizations` | `Organization` | Tenant/account boundary. |
| `organization_members` | `OrganizationMember` | User membership and organization role. |
| `organization_invites` | `OrganizationInvite` | Pending invitation records. |
| `organization_project_access` | `OrganizationProjectAccess` | Member-level project access overrides. |
| `organization_invite_project_access` | `OrganizationInviteProjectAccess` | Invite-time project access grants. |

## Work Structure

| Table | Model | Purpose |
| --- | --- | --- |
| `workspaces` | `Workspace` | Grouping layer above projects. |
| `projects` | `Project` | Main work/project boundary formerly called board. |
| `tasks` | `Task` | Project-scoped work items. |
| `task_dependencies` | `TaskDependency` | Directed task dependency edges. |
| `task_custom_fields` | `TaskCustomFieldDefinition` | Project-scoped custom field definitions. |
| `task_fingerprints` | `TaskFingerprint` | Deduplication/fingerprint records. |
| `tags` | `Tag` | Organization/project labels. |
| `tag_assignments` | `TagAssignment` | Tag links to tasks. |

## Agent And Gateway Runtime

| Table | Model | Purpose |
| --- | --- | --- |
| `gateways` | `Gateway` | Connected OpenClaw gateway configuration. |
| `agents` | `Agent` | Agent identity, lifecycle, token hash, heartbeat state. |
| `project_memory` | `ProjectMemory` | Project-scoped memory/chat records. |
| `workspace_memory` | `WorkspaceMemory` | Workspace-scoped memory records. |
| `project_onboarding` | `ProjectOnboardingSession` | Onboarding session state. |
| `project_webhooks` | `ProjectWebhook` | Project webhook definitions. |
| `project_webhook_payloads` | `ProjectWebhookPayload` | Stored webhook payloads. |

## Governance And Audit

| Table | Model | Purpose |
| --- | --- | --- |
| `approvals` | `Approval` | Human approval requests and decisions. |
| `approval_task_links` | `ApprovalTaskLink` | Many-to-many approval/task association. |
| `activity_events` | `ActivityEvent` | Audit/activity timeline. |
| `skills` | `MarketplaceSkill`, `SkillPack`, `InstalledSkill` | Skill marketplace and install state. |

## Ownership Columns

Several business tables include `created_by` to support ownership scoping:

- `projects`
- `agents`
- `gateways`
- `tags`
- `workspaces`
- `approvals`

Tasks use `created_by_user_id` and `created_by_agent_id` because tasks can be
created by either human users or agents.

## Task Lifecycle Fields

Tasks carry the core fields needed by the operator workflow:

| Field | Meaning |
| --- | --- |
| `title` | Short task name. |
| `description` | Task body/context. |
| `status` | One of `inbox`, `in_progress`, `review`, `done`. |
| `status_reason` | Human-readable reason for the current status. |
| `output` | Required deliverable/output text before a task may be marked `done`. |
| `change_log` | JSON activity snapshots appended by task create/update flows. |
| `in_progress_at` | Start timestamp for active work. |
| `completed_at` | End timestamp when the task first enters `done`. |
| `assigned_agent_id` | Current assignee. |
| `created_by_user_id` | Human creator when user-created. |
| `created_by_agent_id` | Agent creator when agent-created. |

Custom fields can add project-specific output gates with
`task_custom_field_definitions.required_for_done`, but `tasks.output` is the
first-class required output field for the task itself.

## Naming Migration

The schema has moved from board terminology to project/workspace terminology.
The migration `b0a1d2e3f4a5_rename_board_to_project.py` handles table and column
renames.

Legacy model files still exist as shims:

| Legacy shim | Current model |
| --- | --- |
| `models/boards.py` | `infrastructure/models/projects.py` |
| `models/board_groups.py` | `infrastructure/models/workspaces.py` |
| `models/board_memory.py` | `infrastructure/models/project_memory.py` |
| `models/board_webhooks.py` | `infrastructure/models/project_webhooks.py` |
| `models/organization_board_access.py` | `infrastructure/models/organization_project_access.py` |

New migrations and new code should use current names.

## Migration Guidance

- Generate migrations against PostgreSQL-compatible SQLAlchemy metadata.
- Keep data-preserving renames explicit; avoid drop/recreate for renamed
  tables or columns.
- When adding a non-nullable column to existing tables, use a staged migration:
  add nullable column, backfill, then enforce not-null if required.
- Keep compatibility aliases until the final shim-removal phase is complete.

## Relationship Notes

- `projects.organization_id` is the tenant boundary for project-scoped data.
- `projects.workspace_id` associates a project with a workspace when present.
- `projects.gateway_id` identifies the gateway used for agent provisioning.
- `agents.project_id = NULL` means gateway-main agent; otherwise the agent is
  project-scoped.
- `agents.agent_token_hash` stores a hash only. Raw tokens are rendered to
  workspace files and are not recoverable from the database.
- `approvals.task_id` is a legacy single-task reference; the normalized
  multi-task relationship is `approval_task_links`.
