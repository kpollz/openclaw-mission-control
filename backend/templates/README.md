# Backend Templates (Product Documentation)

This folder contains the Markdown templates Mission Control syncs into OpenClaw agent workspaces.

- Location in repo: `backend/templates/`
- Runtime location in backend container: `/app/templates`
- Render engine: Jinja2

## What this is for

Use these templates to control what an agent sees in workspace files like:

- `AGENTS.md`
- `HEARTBEAT.md`
- `TOOLS.md`
- `IDENTITY.md`
- `USER.md`
- `MEMORY.md`

When a gateway template sync runs, these templates are rendered with agent/project context and written into each workspace.

## How rendering works

### Rendering configuration

Defined in `backend/app/services/openclaw/provisioning.py` (`_template_env()`):

- `StrictUndefined` enabled (missing variables fail fast)
- `autoescape=False` (Markdown output)
- `keep_trailing_newline=True`

### Context builders

- Project agent context: `_build_context()`
- Main agent context: `_build_main_context()`
- User mapping: `_user_context()`
- Identity mapping: `_identity_context()`

## Sync entry points

### API

`POST /api/v1/gateways/{gateway_id}/templates/sync`

- Router: `backend/app/api/gateways.py` (`sync_gateway_templates`)
- Service: `backend/app/services/openclaw/provisioning_db.py`

### Script

`backend/scripts/sync_gateway_templates.py`

Example:

```bash
python backend/scripts/sync_gateway_templates.py --gateway-id <uuid>
```

## Files included in sync

Project-agent default synced files are defined in:

- `backend/app/services/openclaw/constants.py` (`DEFAULT_GATEWAY_FILES`)

Project-lead file contract is defined in:

- `backend/app/services/openclaw/constants.py` (`LEAD_GATEWAY_FILES`)

Lead-only override mapping (when needed) is defined in:

- `backend/app/services/openclaw/constants.py` (`LEAD_TEMPLATE_MAP`)

Shared project-agent mapping (lead + non-lead) is defined in:

- `backend/app/services/openclaw/constants.py` (`PROJECT_SHARED_TEMPLATE_MAP`)

Main-agent template mapping is defined in:

- `backend/app/services/openclaw/constants.py` (`MAIN_TEMPLATE_MAP`)

Provisioning selection logic is implemented in:

- `backend/app/services/openclaw/provisioning.py`
  - `ProjectAgentLifecycleManager._file_names()`
  - `ProjectAgentLifecycleManager._template_overrides()`
  - `GatewayMainAgentLifecycleManager._template_overrides()`

Lead-only stale template files are cleaned up during sync by:

- `ProjectAgentLifecycleManager._stale_file_candidates()`

## HEARTBEAT.md selection logic

All agent types (main + project lead + project non-lead) render `HEARTBEAT.md` from:

- `PROJECT_HEARTBEAT.md.j2` via `PROJECT_SHARED_TEMPLATE_MAP`

Role-specific behavior is controlled inside that template with:
- `is_main_agent`
- `is_project_lead`

## OpenAPI refresh location

Lead OpenAPI download/index generation is intentionally documented in:

- `PROJECT_TOOLS.md.j2`

This avoids relying on startup hooks to populate `api/openapi.json`.

## Template variables reference

### Core keys (all templates)

- `agent_name`, `agent_id`, `session_key`
- `base_url`, `auth_token`, `main_session_key`
- `workspace_root`

### User keys

- `user_name`, `user_preferred_name`, `user_pronouns`, `user_timezone`
- `user_notes`, `user_context`

### Identity keys

- `identity_role`, `identity_communication_style`, `identity_emoji`
- `identity_autonomy_level`, `identity_verbosity`, `identity_output_format`, `identity_update_cadence`
- `identity_purpose`, `identity_personality`, `identity_custom_instructions`

### Project-agent-only keys

- `project_id`, `project_name`, `project_type`
- `project_objective`, `project_success_metrics`, `project_target_date`
- `project_goal_confirmed`, `is_project_lead`
- `workspace_path`
- `project_rule_require_approval_for_done`
- `project_rule_require_review_before_done`
- `project_rule_comment_required_for_review`
- `project_rule_block_status_changes_with_pending_approval`
- `project_rule_only_lead_can_change_status`
- `project_rule_max_agents`

## OpenAPI role tags for agents

Agent-facing endpoints expose role tags in OpenAPI so heartbeat files can filter
operations without path regex hacks:

- `agent-lead`: project lead workflows (delegation/review/coordination)
- `agent-worker`: non-lead project execution workflows
- `agent-main`: gateway main / cross-project control-plane workflows

Example filter:

```bash
curl -s "$BASE_URL/openapi.json" \
  | jq -r '.paths | to_entries[] | .key as $path
    | .value | to_entries[]
    | select((.value.tags // []) | index("agent-lead"))
    | "\(.key|ascii_upcase)\t\($path)\t\(.value.operationId // "-")"'
```

## Safe change checklist

Before merging template changes:

1. Do not introduce new `{{ var }}` placeholders unless context builders provide them.
2. Keep changes additive where possible.
3. Review worker (`DEFAULT_*`), lead (`LEAD_*`), and `MAIN_*` templates when changing shared behavior.
4. Preserve agent-editable files behavior (`PRESERVE_AGENT_EDITABLE_FILES`).
5. Run docs quality checks and CI.
6. Keep heartbeat templates under injected-context size limits (20,000 chars each).

## Local validation

### Fast check

Run CI-relevant docs checks locally:

```bash
make docs-check
```

### Full validation

- Push branch
- Confirm PR checks are green
- Optionally run template sync on a dev gateway and inspect generated workspace files

## FAQ

### Why did rendering fail after adding a variable?

Because `StrictUndefined` is enabled. Add that key to `_build_context()` / `_build_main_context()` (and related mappers) before using it in templates.

### Why didn’t my edit appear in an agent workspace?

Template sync may not have run yet, or the target file is preserved as agent-editable. Check sync status and preservation rules in constants.
