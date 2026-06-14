"""Project onboarding service for user/agent collaboration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlmodel import col

from app.shared.config import settings
from app.shared.logging import get_logger
from app.shared.time import utcnow
from app.infrastructure.models.project_onboarding import ProjectOnboardingSession
from app.presentation.schemas.project_onboarding import (
    ProjectOnboardingAgentComplete,
    ProjectOnboardingAgentQuestion,
    ProjectOnboardingLeadAgentDraft,
    ProjectOnboardingUserProfile,
)
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.gateway.resolver import get_gateway_for_project
from app.application.use_cases.agents.onboarding import ProjectOnboardingMessagingService
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.application.use_cases.agents.provisioning_db import (
    LeadAgentOptions,
    LeadAgentRequest,
    OpenClawProvisioningService,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.auth.clerk_local_auth import AuthContext
    from app.infrastructure.models.agents import Agent
    from app.infrastructure.models.projects import Project
    from app.presentation.schemas.project_onboarding import (
        ProjectOnboardingAgentUpdate,
        ProjectOnboardingConfirm,
    )

logger = get_logger(__name__)


def _parse_draft_user_profile(
    draft_goal: object,
) -> ProjectOnboardingUserProfile | None:
    if not isinstance(draft_goal, dict):
        return None
    raw_profile = draft_goal.get("user_profile")
    if raw_profile is None:
        return None
    try:
        return ProjectOnboardingUserProfile.model_validate(raw_profile)
    except ValidationError:
        return None


def _parse_draft_lead_agent(
    draft_goal: object,
) -> ProjectOnboardingLeadAgentDraft | None:
    if not isinstance(draft_goal, dict):
        return None
    raw_lead = draft_goal.get("lead_agent")
    if raw_lead is None:
        return None
    try:
        return ProjectOnboardingLeadAgentDraft.model_validate(raw_lead)
    except ValidationError:
        return None


def _normalize_autonomy_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    return text.replace("_", "-")


def _is_fully_autonomous_choice(value: object) -> bool:
    token = _normalize_autonomy_token(value)
    if token is None:
        return False
    if token in {"autonomous", "fully-autonomous", "full-autonomy"}:
        return True
    return "autonom" in token and "fully" in token


def _require_approval_for_done_from_draft(draft_goal: object) -> bool:
    """Enable done-approval gate unless onboarding selected fully autonomous mode."""
    if not isinstance(draft_goal, dict):
        return True
    raw_lead = draft_goal.get("lead_agent")
    if not isinstance(raw_lead, dict):
        return True
    if _is_fully_autonomous_choice(raw_lead.get("autonomy_level")):
        return False
    raw_identity_profile = raw_lead.get("identity_profile")
    if isinstance(raw_identity_profile, dict):
        for key in ("autonomy_level", "autonomy", "mode"):
            if _is_fully_autonomous_choice(raw_identity_profile.get(key)):
                return False
    return True


def _apply_user_profile(
    auth: AuthContext,
    profile: ProjectOnboardingUserProfile | None,
) -> bool:
    if auth.user is None or profile is None:
        return False

    changed = False
    if profile.preferred_name is not None:
        auth.user.preferred_name = profile.preferred_name
        changed = True
    if profile.pronouns is not None:
        auth.user.pronouns = profile.pronouns
        changed = True
    if profile.timezone is not None:
        auth.user.timezone = profile.timezone
        changed = True
    if profile.notes is not None:
        auth.user.notes = profile.notes
        changed = True
    if profile.context is not None:
        auth.user.context = profile.context
        changed = True
    return changed


def _lead_agent_options(
    lead_agent: ProjectOnboardingLeadAgentDraft | None,
) -> LeadAgentOptions:
    if lead_agent is None:
        return LeadAgentOptions(action="provision")

    lead_identity_profile: dict[str, str] = {}
    if lead_agent.identity_profile:
        lead_identity_profile.update(lead_agent.identity_profile)
    if lead_agent.autonomy_level:
        lead_identity_profile["autonomy_level"] = lead_agent.autonomy_level
    if lead_agent.verbosity:
        lead_identity_profile["verbosity"] = lead_agent.verbosity
    if lead_agent.output_format:
        lead_identity_profile["output_format"] = lead_agent.output_format
    if lead_agent.update_cadence:
        lead_identity_profile["update_cadence"] = lead_agent.update_cadence
    if lead_agent.custom_instructions:
        lead_identity_profile["custom_instructions"] = lead_agent.custom_instructions

    return LeadAgentOptions(
        agent_name=lead_agent.name,
        identity_profile=lead_identity_profile or None,
        action="provision",
    )


class ProjectOnboardingService:
    """Facade for project onboarding lifecycle management."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _is_question_message(message: object) -> bool:
        """Return True when a stored assistant message is a pending question.

        A question payload has a ``question`` field; a completion payload carries
        ``status=complete`` instead. We treat only the former as "awaiting answer".
        """
        if not isinstance(message, dict):
            return False
        if message.get("role") != "assistant":
            return False
        content = message.get("content")
        if not isinstance(content, str) or not content:
            return False
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        if data.get("status") == "complete":
            return False
        return bool(data.get("question"))

    @classmethod
    def _has_unanswered_question(cls, messages: list[dict[str, object]]) -> bool:
        """Return True when the latest message is an unanswered assistant question.

        Used to enforce the one-question-at-a-time protocol: if the most recent
        message is a question, the user has not answered yet (a user answer would
        be appended after it), so the agent must wait.
        """
        if not messages:
            return False
        return cls._is_question_message(messages[-1])

    @staticmethod
    def _question_text(message: object) -> str | None:
        """Extract the human-readable question text from an assistant message."""
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, str) or not content:
            return None
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        question = data.get("question")
        return question if isinstance(question, str) and question else None

    @classmethod
    def _build_answer_dispatch_message(
        cls,
        *,
        messages: list[dict[str, object]],
        current_answer: str,
    ) -> str:
        """Build an enriched message for the agent when the user answers.

        Sends the agent (1) the history of previously answered questions and
        (2) the current question paired with the user's latest answer, so the
        agent has full context to decide the single next question.
        """
        # Walk the transcript and pair each assistant question with the user
        # message that immediately follows it (its answer).
        answered_pairs: list[tuple[str, str]] = []
        current_question: str | None = None
        pending_question: str | None = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                question = cls._question_text(message)
                if question is not None:
                    pending_question = question
            elif role == "user":
                content = message.get("content")
                if pending_question is not None and isinstance(content, str):
                    answered_pairs.append((pending_question, content))
                    pending_question = None
        # The still-pending question (no answer yet in the stored transcript) is
        # the one the user is answering right now.
        current_question = pending_question

        lines: list[str] = ["ONBOARDING ANSWER RECEIVED", ""]
        if answered_pairs:
            lines.append("Previously answered questions:")
            for index, (question, answer) in enumerate(answered_pairs, start=1):
                lines.append(f"{index}. Q: {question}")
                lines.append(f"   A: {answer}")
            lines.append("")
        lines.append("Latest question and answer:")
        if current_question is not None:
            lines.append(f"Q: {current_question}")
        lines.append(f"A: {current_answer}")
        lines.append("")
        lines.append(
            "Based on the full context above, decide the SINGLE next question and "
            "send it (one question only). Then STOP and wait for the user's answer "
            "before sending anything else. If you have enough information, send the "
            "completion payload (status=complete) instead."
        )
        return "\n".join(lines)

    async def get_onboarding(
        self,
        *,
        project: Project,
    ) -> ProjectOnboardingSession:
        """Get the latest onboarding session for a project."""
        onboarding = (
            await ProjectOnboardingSession.objects.filter_by(project_id=project.id)
            .order_by(col(ProjectOnboardingSession.updated_at).desc())
            .first(self._session)
        )
        if onboarding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return onboarding

    async def start_onboarding(
        self,
        *,
        project: Project,
    ) -> ProjectOnboardingSession:
        """Start onboarding and send instructions to the gateway agent."""
        session = self._session
        onboarding = (
            await ProjectOnboardingSession.objects.filter_by(project_id=project.id)
            .filter(col(ProjectOnboardingSession.status) == "active")
            .first(session)
        )
        if onboarding:
            last_user_content: str | None = None
            messages = onboarding.messages or []
            if messages:
                last_message = messages[-1]
                if isinstance(last_message, dict):
                    last_role = last_message.get("role")
                    content = last_message.get("content")
                    if last_role == "user" and isinstance(content, str) and content:
                        last_user_content = content

            if last_user_content:
                dispatcher = ProjectOnboardingMessagingService(session)
                await dispatcher.dispatch_answer(
                    project=project,
                    onboarding=onboarding,
                    answer_text=last_user_content,
                    correlation_id=f"onboarding.resume:{project.id}:{onboarding.id}",
                )
                onboarding.updated_at = utcnow()
                session.add(onboarding)
                await session.commit()
                await session.refresh(onboarding)
            return onboarding

        dispatcher = ProjectOnboardingMessagingService(session)
        base_url = settings.base_url
        prompt = (
            "BOARD ONBOARDING REQUEST\n\n"
            f"Project Name: {project.name}\n"
            f"Project Description: {project.description or '(not provided)'}\n"
            "You are the gateway agent. Ask the user 6-10 focused questions total.\n\n"
            "CRITICAL PROTOCOL — ASK ONE QUESTION AT A TIME:\n"
            "- Send EXACTLY ONE question per API call, then STOP and WAIT.\n"
            "- Do NOT send the next question until you receive the user's answer "
            "for the current one.\n"
            "- The server enforces this: if you send a new question while the "
            "previous one is unanswered, it returns HTTP 409 and rejects it. "
            "If you get a 409, wait for the answer instead of retrying.\n"
            "- When the user answers, Mission Control sends you the full history "
            "of answered questions plus the latest question/answer pair. Use that "
            "context to decide the single next question.\n\n"
            "Question budget:\n"
            "- 3-6 questions to clarify the project goal.\n"
            "- 1 question to choose a unique name for the project lead agent "
            "(first-name style).\n"
            "- 2-4 questions to capture the user's preferences for how the project "
            "lead should work\n"
            "  (communication style, autonomy, update cadence, and output formatting).\n"
            '- Always include a final question (and only once): "Anything else we '
            'should know?"\n'
            "  (constraints, context, preferences). This MUST be the last question.\n"
            '  Provide an option like "Yes (I\'ll type it)" so they can enter free-text.\n'
            "  Do NOT ask for additional context on earlier questions.\n"
            "  Only include a free-text option on earlier questions if a typed "
            "answer is necessary;\n"
            '  when you do, make the option label include "I\'ll type it" '
            '(e.g., "Other (I\'ll type it)").\n'
            '- If the user sends an "Additional context" message later, incorporate '
            "it and resend status=complete\n"
            "  to update the draft (until the user confirms).\n"
            "Do NOT respond in OpenClaw chat.\n"
            "All onboarding responses MUST be sent to Mission Control via API.\n"
            f"Mission Control base URL: {base_url}\n"
            "To call the API, follow `skills/mission-control/SKILL.md`: read your credential "
            "file, then run curl with the base_url and auth_token written straight into the "
            "command (no shell variables, no $(...)).\n"
            "ALWAYS send the JSON body via a temp file, never with -d '...': write it with a "
            "quoted heredoc (cat > /tmp/onb.json <<'JSON' ... JSON) then --data @/tmp/onb.json. "
            "Inlining -d '...' breaks on any apostrophe (e.g. \"I'll type it\").\n"
            "Onboarding response endpoint:\n"
            f"POST {base_url}/api/v1/agent/projects/{project.id}/onboarding\n"
            "QUESTION JSON body (send exactly this shape):\n"
            '{"question":"...","options":[{"id":"1","label":"..."},'
            '{"id":"2","label":"..."}]}\n'
            "COMPLETION JSON body (send exactly this shape):\n"
            '{"status":"complete","project_type":"goal","objective":"...",'
            '"success_metrics":{"metric":"...","target":"..."},'
            '"target_date":"YYYY-MM-DD",'
            '"user_profile":{"preferred_name":"...","pronouns":"...",'
            '"timezone":"...","notes":"...","context":"..."},'
            '"lead_agent":{"name":"Ava","identity_profile":{"role":"Project Lead",'
            '"communication_style":"direct, concise, practical","emoji":":gear:"},'
            '"autonomy_level":"balanced","verbosity":"concise",'
            '"output_format":"bullets","update_cadence":"daily",'
            '"custom_instructions":"..."}}\n'
            "ENUMS:\n"
            "- project_type: goal | general\n"
            "- lead_agent.autonomy_level: ask_first | balanced | autonomous\n"
            "- lead_agent.verbosity: concise | balanced | detailed\n"
            "- lead_agent.output_format: bullets | mixed | narrative\n"
            "- lead_agent.update_cadence: asap | hourly | daily | weekly\n"
            "QUESTION FORMAT (one question per response, no arrays, no markdown, "
            "no extra text):\n"
            '{"question":"...","options":[{"id":"1","label":"..."},{"id":"2","label":"..."}]}\n'
            "Do NOT wrap questions in a list. Do NOT add commentary.\n"
            "Send ONE question, then STOP and WAIT for the answer before sending "
            "the next one (the server rejects early questions with HTTP 409).\n"
            "When you have enough info, send one final response with status=complete.\n"
            "The completion payload must include project_type. If project_type=goal, "
            "include objective + success_metrics.\n"
            "Also include user_profile + lead_agent to configure the project lead's "
            "working style.\n"
        )

        session_key = await dispatcher.dispatch_start_prompt(
            project=project,
            prompt=prompt,
            correlation_id=f"onboarding.start:{project.id}",
        )

        onboarding = ProjectOnboardingSession(
            project_id=project.id,
            session_key=session_key,
            status="active",
            messages=[
                {"role": "user", "content": prompt, "timestamp": utcnow().isoformat()},
            ],
        )
        session.add(onboarding)
        await session.commit()
        await session.refresh(onboarding)
        return onboarding

    async def answer_onboarding(
        self,
        *,
        project: Project,
        answer: str,
        other_text: str | None = None,
    ) -> ProjectOnboardingSession:
        """Send a user onboarding answer to the gateway agent."""
        session = self._session
        onboarding = (
            await ProjectOnboardingSession.objects.filter_by(project_id=project.id)
            .order_by(col(ProjectOnboardingSession.updated_at).desc())
            .first(session)
        )
        if onboarding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        dispatcher = ProjectOnboardingMessagingService(session)
        answer_text = answer
        if other_text:
            answer_text = f"{answer}: {other_text}"

        messages = list(onboarding.messages or [])
        # Build the enriched agent message from the transcript BEFORE appending
        # the new answer, so the pending question is paired with this answer.
        dispatch_message = self._build_answer_dispatch_message(
            messages=messages,
            current_answer=answer_text,
        )
        messages.append(
            {"role": "user", "content": answer_text, "timestamp": utcnow().isoformat()},
        )

        await dispatcher.dispatch_answer(
            project=project,
            onboarding=onboarding,
            answer_text=dispatch_message,
            correlation_id=f"onboarding.answer:{project.id}:{onboarding.id}",
        )

        onboarding.messages = messages
        onboarding.updated_at = utcnow()
        session.add(onboarding)
        await session.commit()
        await session.refresh(onboarding)
        return onboarding

    async def agent_onboarding_update(
        self,
        *,
        project: Project,
        agent: Agent,
        payload: ProjectOnboardingAgentUpdate,
    ) -> ProjectOnboardingSession:
        """Store onboarding updates submitted by the gateway agent."""
        session = self._session

        OpenClawAuthorizationPolicy.require_gateway_scoped_actor(actor_agent=agent)

        gateway = await get_gateway_for_project(session, project)
        if gateway is not None:
            from app.infrastructure.gateway.shared import GatewayAgentIdentity

            session_key = GatewayAgentIdentity.session_key(gateway)
            OpenClawAuthorizationPolicy.require_gateway_main_actor_binding(
                actor_agent=agent,
                gateway=gateway,
                gateway_session_key=session_key,
            )

        onboarding = (
            await ProjectOnboardingSession.objects.filter_by(project_id=project.id)
            .order_by(col(ProjectOnboardingSession.updated_at).desc())
            .first(session)
        )
        if onboarding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if onboarding.status == "confirmed":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)

        messages = list(onboarding.messages or [])

        # Guard: enforce a strict one-question-at-a-time protocol. If the agent
        # already asked a question that the user has not answered yet (i.e. the
        # most recent message is an assistant question), reject any new question
        # so the agent must wait for the user's answer before continuing.
        if isinstance(payload, ProjectOnboardingAgentQuestion) and self._has_unanswered_question(
            messages
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A previous onboarding question is still awaiting the user's answer. "
                    "Send exactly one question, then STOP and wait. Do not send the next "
                    "question until you receive the user's answer for the current one."
                ),
            )

        now = utcnow().isoformat()
        payload_text = payload.model_dump_json(exclude_none=True)
        payload_data = payload.model_dump(mode="json", exclude_none=True)
        logger.info(
            "onboarding.agent.update project_id=%s agent_id=%s payload=%s",
            project.id,
            agent.id,
            payload_text,
        )
        if isinstance(payload, ProjectOnboardingAgentComplete):
            onboarding.draft_goal = payload_data
            onboarding.status = "completed"
            messages.append(
                {"role": "assistant", "content": payload_text, "timestamp": now},
            )
        else:
            messages.append(
                {"role": "assistant", "content": payload_text, "timestamp": now},
            )

        onboarding.messages = messages
        onboarding.updated_at = utcnow()
        session.add(onboarding)
        await session.commit()
        await session.refresh(onboarding)
        logger.info(
            "onboarding.agent.update stored project_id=%s messages_count=%s status=%s",
            project.id,
            len(onboarding.messages or []),
            onboarding.status,
        )
        return onboarding

    async def confirm_onboarding(
        self,
        *,
        project: Project,
        auth: AuthContext,
        payload: ProjectOnboardingConfirm,
    ) -> Project:
        """Confirm onboarding results and provision the project lead agent."""
        session = self._session
        onboarding = (
            await ProjectOnboardingSession.objects.filter_by(project_id=project.id)
            .order_by(col(ProjectOnboardingSession.updated_at).desc())
            .first(session)
        )
        if onboarding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        project.project_type = payload.project_type
        project.objective = payload.objective
        project.success_metrics = payload.success_metrics
        project.target_date = payload.target_date
        project.goal_confirmed = True
        project.goal_source = "lead_agent_onboarding"
        project.require_approval_for_done = _require_approval_for_done_from_draft(
            onboarding.draft_goal,
        )

        onboarding.status = "confirmed"
        onboarding.updated_at = utcnow()

        user_profile = _parse_draft_user_profile(onboarding.draft_goal)
        if _apply_user_profile(auth, user_profile) and auth.user is not None:
            session.add(auth.user)

        lead_agent = _parse_draft_lead_agent(onboarding.draft_goal)
        lead_options = _lead_agent_options(lead_agent)

        gateway, config = await GatewayDispatchService(session).require_gateway_config_for_project(project)
        session.add(project)
        session.add(onboarding)
        await session.commit()
        await session.refresh(project)
        await OpenClawProvisioningService(session).ensure_project_lead_agent(
            request=LeadAgentRequest(
                project=project,
                gateway=gateway,
                config=config,
                user=auth.user,
                options=lead_options,
            ),
        )
        return project
