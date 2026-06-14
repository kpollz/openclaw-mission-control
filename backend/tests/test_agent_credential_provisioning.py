# ruff: noqa
"""Verify the agent-owned credential file + mission-control skill delivery.

Contract under test:
- Mission Control does NOT push the credential or skill as gateway files (the gateway
  file API allowlists only the markdown workspace files). Instead it delivers the
  credential JSON inside the provision/wakeup message and serves the skill over HTTP.
- The agent persists the credential to an absolute path in its own workspace and reads
  it with `jq`; `TOOLS.md`/`HEARTBEAT.md`/`BOOTSTRAP.md` reference that absolute path and
  never inline the token.
- Every system message dispatched to a project agent is suffixed with the footer.
"""

from __future__ import annotations

import json

import pytest

import app.infrastructure.gateway.provisioner as agent_provisioning
from app.infrastructure.gateway.constants import (
    DEFAULT_GATEWAY_FILES,
    LEAD_GATEWAY_FILES,
    MAIN_TEMPLATE_MAP,
    MISSION_CONTROL_CREDENTIAL_FILE,
    PROJECT_SHARED_TEMPLATE_MAP,
)
from app.infrastructure.gateway.dispatch import (
    GatewayDispatchService,
    mission_control_agent_footer,
)


# ---------------------------------------------------------------------------
# Rendering context fixtures
# ---------------------------------------------------------------------------

_WORKSPACE_PATH = "~/.openclaw/workspace-alice"
_CREDENTIAL_PATH = f"{_WORKSPACE_PATH}/mission_control_credential.json"


def _worker_context() -> dict[str, str]:
    return {
        "agent_name": "Alice",
        "agent_id": "agent-1",
        "project_id": "proj-1",
        "project_name": "Demo Project",
        "project_type": "standard",
        "project_objective": "",
        "project_success_metrics": "{}",
        "project_target_date": "",
        "project_goal_confirmed": "false",
        "project_rule_require_approval_for_done": "false",
        "project_rule_require_review_before_done": "false",
        "project_rule_comment_required_for_review": "false",
        "project_rule_block_status_changes_with_pending_approval": "false",
        "project_rule_only_lead_can_change_status": "false",
        "project_rule_max_agents": "5",
        "is_project_lead": "false",
        "is_main_agent": "false",
        "session_key": "agent:alice:main",
        "workspace_path": _WORKSPACE_PATH,
        "credential_path": _CREDENTIAL_PATH,
        "base_url": "http://localhost:9999",
        "auth_token": "TKN-SECRET-WORKER",
        "main_session_key": "gateway-main",
        "workspace_root": "~/.openclaw",
        "user_name": "",
        "user_preferred_name": "",
        "user_pronouns": "",
        "user_timezone": "",
        "user_notes": "",
        "user_context": "",
        "identity_role": "Generalist",
        "identity_communication_style": "direct",
        "identity_emoji": ":gear:",
        "identity_autonomy_level": "",
        "identity_verbosity": "",
        "identity_output_format": "",
        "identity_update_cadence": "",
        "identity_purpose": "",
        "identity_personality": "",
        "identity_custom_instructions": "",
        "directory_role_soul_markdown": "",
        "directory_role_soul_source_url": "",
    }


def _lead_context() -> dict[str, str]:
    ctx = _worker_context()
    ctx["is_project_lead"] = "true"
    return ctx


def _main_context() -> dict[str, str]:
    return {
        "agent_name": "Acme Gateway",
        "agent_id": "main-1",
        "is_main_agent": "true",
        "is_project_lead": "false",
        "session_key": "gateway-main",
        "workspace_path": "~/.openclaw/workspace-gateway-1",
        "credential_path": "~/.openclaw/workspace-gateway-1/mission_control_credential.json",
        "base_url": "http://localhost:9999",
        "auth_token": "TKN-MAIN-SECRET",
        "main_session_key": "gateway-main",
        "workspace_root": "~/.openclaw",
        "user_name": "",
        "user_preferred_name": "",
        "user_pronouns": "",
        "user_timezone": "",
        "user_notes": "",
        "user_context": "",
        "identity_role": "Generalist",
        "identity_communication_style": "direct",
        "identity_emoji": ":gear:",
        "identity_autonomy_level": "",
        "identity_verbosity": "",
        "identity_output_format": "",
        "identity_update_cadence": "",
        "identity_purpose": "",
        "identity_personality": "",
        "identity_custom_instructions": "",
    }


def _render(template_name: str, context: dict[str, str]) -> str:
    env = agent_provisioning._template_env()
    return env.get_template(template_name).render(**context).strip()


# ---------------------------------------------------------------------------
# Credential JSON builder (delivered via the wakeup message)
# ---------------------------------------------------------------------------


def test_credential_json_is_valid_and_complete_for_worker():
    raw = agent_provisioning._credential_json(
        base_url="http://localhost:9999",
        auth_token="TKN-SECRET-WORKER",
        agent_id="agent-1",
        agent_name="Alice",
        role="worker",
        project_id="proj-1",
        workspace_path=_WORKSPACE_PATH,
    )
    parsed = json.loads(raw)
    assert parsed["auth_token"] == "TKN-SECRET-WORKER"
    assert parsed["agent_id"] == "agent-1"
    assert parsed["project_id"] == "proj-1"
    assert parsed["role"] == "worker"
    assert parsed["auth_header"] == "X-Agent-Token"
    assert parsed["base_url"] == "http://localhost:9999"
    assert parsed["workspace_path"] == _WORKSPACE_PATH


def test_credential_json_omits_project_id_for_main():
    raw = agent_provisioning._credential_json(
        base_url="http://localhost:9999",
        auth_token="TKN-MAIN",
        agent_id="main-1",
        agent_name="Acme",
        role="main",
        project_id=None,
        workspace_path="~/.openclaw/workspace-gateway-1",
    )
    parsed = json.loads(raw)
    assert parsed["role"] == "main"
    assert "project_id" not in parsed


def test_credential_path_appends_filename():
    assert (
        agent_provisioning._credential_path("~/.openclaw/workspace-alice/")
        == _CREDENTIAL_PATH
    )


# ---------------------------------------------------------------------------
# We no longer push credential/skill files via the gateway file API
# ---------------------------------------------------------------------------


def test_credential_and_skill_not_registered_as_gateway_files():
    for fname in (
        MISSION_CONTROL_CREDENTIAL_FILE,
        "skills/mission-control/SKILL.md",
        "skills/mission-control/references/api_schema.md",
    ):
        assert fname not in DEFAULT_GATEWAY_FILES
        assert fname not in LEAD_GATEWAY_FILES
        assert fname not in PROJECT_SHARED_TEMPLATE_MAP
        assert fname not in MAIN_TEMPLATE_MAP


# ---------------------------------------------------------------------------
# Token-free templates that point at the absolute credential path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_name",
    [
        "PROJECT_TOOLS.md.j2",
        "PROJECT_HEARTBEAT.md.j2",
        "PROJECT_BOOTSTRAP.md.j2",
    ],
)
def test_workspace_templates_use_absolute_credential_path_without_token(template_name: str):
    rendered = _render(template_name, _worker_context())
    assert "TKN-SECRET-WORKER" not in rendered
    assert _CREDENTIAL_PATH in rendered


def test_lead_tools_and_heartbeat_reference_credential_path():
    ctx = _lead_context()
    tools = _render("PROJECT_TOOLS.md.j2", ctx)
    heartbeat = _render("PROJECT_HEARTBEAT.md.j2", ctx)
    assert "TKN-SECRET-WORKER" not in tools
    assert "TKN-SECRET-WORKER" not in heartbeat
    assert _CREDENTIAL_PATH in tools
    assert _CREDENTIAL_PATH in heartbeat


def test_main_tools_and_heartbeat_reference_credential_path():
    ctx = _main_context()
    tools = _render("PROJECT_TOOLS.md.j2", ctx)
    heartbeat = _render("PROJECT_HEARTBEAT.md.j2", ctx)
    assert "TKN-MAIN-SECRET" not in tools
    assert "TKN-MAIN-SECRET" not in heartbeat
    assert ctx["credential_path"] in tools
    assert ctx["credential_path"] in heartbeat


def test_tools_points_to_skill_as_single_source():
    tools = _render("PROJECT_TOOLS.md.j2", _worker_context())
    # TOOLS no longer teaches API mechanics; it points at the skill.
    assert "skills/mission-control/SKILL.md" in tools
    assert "$(jq" not in tools
    assert "AUTH_TOKEN=" not in tools


# ---------------------------------------------------------------------------
# Skill rendering (served over HTTP)
# ---------------------------------------------------------------------------


def test_render_skill_document_role_specific():
    worker = agent_provisioning.render_mission_control_skill_document(
        "SKILL.md", is_main=False, is_lead=False, project_id="proj-1"
    )
    assert "agent-worker" in worker
    assert "TKN" not in worker

    lead = agent_provisioning.render_mission_control_skill_document(
        "SKILL.md", is_main=False, is_lead=True, project_id="proj-1"
    )
    assert "agent-lead" in lead

    main = agent_provisioning.render_mission_control_skill_document(
        "SKILL.md", is_main=True, is_lead=False, project_id=None
    )
    assert "agent-main" in main


def test_render_skill_api_schema_includes_base_url():
    rendered = agent_provisioning.render_mission_control_skill_document(
        "references/api_schema.md",
        is_main=False,
        is_lead=False,
        project_id="proj-1",
        base_url="http://localhost:9999",
    )
    assert "http://localhost:9999" in rendered
    assert "X-Agent-Token" in rendered


def test_render_skill_document_rejects_unknown_name():
    with pytest.raises(KeyError):
        agent_provisioning.render_mission_control_skill_document(
            "../secrets.md", is_main=False, is_lead=False, project_id=None
        )


# ---------------------------------------------------------------------------
# Wakeup message carries the credential write instruction
# ---------------------------------------------------------------------------


def test_wakeup_text_embeds_credential_write_instruction():
    class _Stub:
        name = "Alice"
        is_project_lead = False

    credential_json = agent_provisioning._credential_json(
        base_url="http://localhost:9999",
        auth_token="TKN-SECRET-WORKER",
        agent_id="agent-1",
        agent_name="Alice",
        role="worker",
        project_id="proj-1",
        workspace_path=_WORKSPACE_PATH,
    )
    text = agent_provisioning._wakeup_text(
        _Stub(),
        verb="provisioned",
        credential_path=_CREDENTIAL_PATH,
        credential_json=credential_json,
        project_id="proj-1",
    )
    assert _CREDENTIAL_PATH in text
    assert "TKN-SECRET-WORKER" in text  # token is delivered here, once
    # New contract: read the credential file then inline values into curl — no $(...)/jq.
    assert "skills/mission-control/SKILL.md" in text
    assert f"cat {_CREDENTIAL_PATH}" in text
    assert "$(jq" not in text
    # A worker gets no Board Chat intro step.
    assert "BOARD CHAT" not in text
    # Backward-compat guarantee preserved from the prior wakeup contract.
    assert "If BOOTSTRAP.md exists, read it first, then read AGENTS.md." in text


def test_wakeup_text_lead_includes_board_chat_intro():
    class _Stub:
        name = "Ava"
        is_project_lead = True

    credential_json = agent_provisioning._credential_json(
        base_url="http://localhost:9999",
        auth_token="TKN-SECRET-LEAD",
        agent_id="agent-lead",
        agent_name="Ava",
        role="lead",
        project_id="proj-7",
        workspace_path=_WORKSPACE_PATH,
    )
    text = agent_provisioning._wakeup_text(
        _Stub(),
        verb="provisioned",
        credential_path=_CREDENTIAL_PATH,
        credential_json=credential_json,
        project_id="proj-7",
    )
    # Lead is told to announce readiness on Board Chat (not OpenClaw chat).
    assert "BOARD CHAT" in text
    assert '"tags":["chat"]' in text
    assert "/api/v1/agent/projects/proj-7/memory" in text
    assert "Ava" in text


def test_wakeup_text_without_credential_is_plain():
    class _Stub:
        name = "Alice"
        is_project_lead = False

    text = agent_provisioning._wakeup_text(_Stub(), verb="updated")
    assert "Hello Alice" in text
    assert "mission_control_credential.json" not in text


# ---------------------------------------------------------------------------
# Footer + dispatch wiring
# ---------------------------------------------------------------------------


def test_mission_control_agent_footer_has_no_token_and_names_required_files():
    footer = mission_control_agent_footer()
    assert "mission_control_credential.json" in footer
    assert "skills/mission-control/SKILL.md" in footer
    # Footer points at the skill; it must not teach a jq-substitution command or carry a token.
    assert "$(jq" not in footer
    assert "jq -r .auth_token" not in footer


@pytest.mark.asyncio
async def test_send_agent_message_appends_footer_when_requested():
    from unittest.mock import patch

    captured: dict[str, str] = {}

    async def fake_ensure_session(*_args, **_kwargs):
        return None

    async def fake_send_message(payload, *, session_key, config, deliver):
        captured["payload"] = payload

    svc = GatewayDispatchService.__new__(GatewayDispatchService)

    with patch(
        "app.infrastructure.gateway.dispatch.ensure_session", new=fake_ensure_session
    ), patch(
        "app.infrastructure.gateway.dispatch.send_message", new=fake_send_message
    ):
        await svc.send_agent_message(
            session_key="agent:alice:main",
            config=object(),  # type: ignore[arg-type]
            agent_name="Alice",
            message="hello",
            append_footer=True,
        )

    assert captured["payload"].startswith("hello")
    assert "mission_control_credential.json" in captured["payload"]
    assert "skills/mission-control/SKILL.md" in captured["payload"]


@pytest.mark.asyncio
async def test_send_agent_message_omits_footer_by_default():
    from unittest.mock import patch

    captured: dict[str, str] = {}

    async def fake_ensure_session(*_args, **_kwargs):
        return None

    async def fake_send_message(payload, *, session_key, config, deliver):
        captured["payload"] = payload

    svc = GatewayDispatchService.__new__(GatewayDispatchService)

    with patch(
        "app.infrastructure.gateway.dispatch.ensure_session", new=fake_ensure_session
    ), patch(
        "app.infrastructure.gateway.dispatch.send_message", new=fake_send_message
    ):
        await svc.send_agent_message(
            session_key="agent:alice:main",
            config=object(),  # type: ignore[arg-type]
            agent_name="Alice",
            message="hello",
        )

    assert captured["payload"] == "hello"
