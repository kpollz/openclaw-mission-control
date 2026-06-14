# ruff: noqa: INP001, S101
"""Tests for the one-question-at-a-time onboarding protocol.

Covers:
- the server-side guard helpers (`_has_unanswered_question`, `_is_question_message`)
- the enriched answer dispatch message (history + latest Q/A pair)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import app.application.use_cases.onboarding.service as onboarding_service
from app.application.use_cases.onboarding.service import ProjectOnboardingService
from app.presentation.schemas.project_onboarding import ProjectOnboardingAgentQuestion
from app.shared.time import utcnow
from app.infrastructure.models.project_onboarding import ProjectOnboardingSession


def _question_msg(text: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": json.dumps(
            {"question": text, "options": [{"id": "1", "label": "Yes"}]}
        ),
        "timestamp": utcnow().isoformat(),
    }


def _user_msg(text: str) -> dict[str, object]:
    return {"role": "user", "content": text, "timestamp": utcnow().isoformat()}


def _complete_msg() -> dict[str, object]:
    return {
        "role": "assistant",
        "content": json.dumps({"status": "complete", "project_type": "general"}),
        "timestamp": utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Guard helpers
# ---------------------------------------------------------------------------


def test_is_question_message_detects_question() -> None:
    assert ProjectOnboardingService._is_question_message(_question_msg("Timezone?")) is True


def test_is_question_message_ignores_completion() -> None:
    assert ProjectOnboardingService._is_question_message(_complete_msg()) is False


def test_is_question_message_ignores_user_message() -> None:
    assert ProjectOnboardingService._is_question_message(_user_msg("UTC")) is False


def test_has_unanswered_question_true_when_last_is_question() -> None:
    messages = [_question_msg("Q1"), _user_msg("A1"), _question_msg("Q2")]
    assert ProjectOnboardingService._has_unanswered_question(messages) is True


def test_has_unanswered_question_false_when_last_is_answer() -> None:
    messages = [_question_msg("Q1"), _user_msg("A1")]
    assert ProjectOnboardingService._has_unanswered_question(messages) is False


def test_has_unanswered_question_false_when_empty() -> None:
    assert ProjectOnboardingService._has_unanswered_question([]) is False


# ---------------------------------------------------------------------------
# Enriched answer dispatch
# ---------------------------------------------------------------------------


def test_build_answer_dispatch_message_pairs_history_and_latest() -> None:
    # Transcript: Q1 answered, Q2 answered, Q3 pending (being answered now).
    messages = [
        {"role": "user", "content": "<<start prompt>>"},
        _question_msg("What is the goal?"),
        _user_msg("Ship v1"),
        _question_msg("What timezone?"),
        _user_msg("UTC"),
        _question_msg("Lead agent name?"),
    ]
    out = ProjectOnboardingService._build_answer_dispatch_message(
        messages=messages,
        current_answer="Ava",
    )
    # History contains the two answered pairs.
    assert "What is the goal?" in out
    assert "Ship v1" in out
    assert "What timezone?" in out
    assert "UTC" in out
    # Latest pairs the pending question with the current answer.
    assert "Lead agent name?" in out
    assert "Ava" in out
    # Instruction enforces single next question + wait.
    assert "SINGLE next question" in out


def test_build_answer_dispatch_message_first_answer_has_no_history() -> None:
    messages = [
        {"role": "user", "content": "<<start prompt>>"},
        _question_msg("What is the goal?"),
    ]
    out = ProjectOnboardingService._build_answer_dispatch_message(
        messages=messages,
        current_answer="Ship v1",
    )
    assert "Previously answered questions:" not in out
    assert "What is the goal?" in out
    assert "Ship v1" in out


# ---------------------------------------------------------------------------
# answer_onboarding dispatches the enriched message
# ---------------------------------------------------------------------------


@dataclass
class _FakeScalarResult:
    value: object | None

    def first(self) -> object | None:
        return self.value


@dataclass
class _FakeSession:
    first_value: object | None
    added: list[object] = field(default_factory=list)
    committed: int = 0
    refreshed: list[object] = field(default_factory=list)

    async def exec(self, _statement: object) -> _FakeScalarResult:
        return _FakeScalarResult(self.first_value)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, value: object) -> None:
        self.refreshed.append(value)


@pytest.mark.asyncio
async def test_answer_onboarding_dispatches_enriched_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    onboarding = ProjectOnboardingSession(
        project_id=project_id,
        session_key="session-key",
        status="active",
        messages=[
            _question_msg("What is the goal?"),
            _user_msg("Ship v1"),
            _question_msg("What timezone?"),
        ],
    )
    session: Any = _FakeSession(first_value=onboarding)
    project = SimpleNamespace(id=project_id, name="Roadmap", description="Build v1")
    captured: list[str] = []

    class _FakeMessagingService:
        def __init__(self, _session: object) -> None:
            self._session = _session

        async def dispatch_answer(
            self,
            *,
            project: object,
            onboarding: object,
            answer_text: str,
            correlation_id: str,
        ) -> None:
            captured.append(answer_text)

    monkeypatch.setattr(
        onboarding_service,
        "ProjectOnboardingMessagingService",
        _FakeMessagingService,
    )

    svc = ProjectOnboardingService(session)
    await svc.answer_onboarding(project=project, answer="UTC")

    assert len(captured) == 1
    dispatched = captured[0]
    # Enriched: includes prior answered pair + current question/answer.
    assert "What is the goal?" in dispatched
    assert "Ship v1" in dispatched
    assert "What timezone?" in dispatched
    assert "UTC" in dispatched
    # The new user answer is appended to the transcript.
    assert onboarding.messages[-1]["role"] == "user"
    assert onboarding.messages[-1]["content"] == "UTC"


# ---------------------------------------------------------------------------
# Server-side guard rejects a second question before the user answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_onboarding_update_rejects_second_question_with_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the latest stored message is an unanswered assistant question, a new
    ProjectOnboardingAgentQuestion must be rejected with HTTP 409 so the agent
    cannot flood the user with multiple consecutive questions.
    """
    project_id = uuid4()
    onboarding = ProjectOnboardingSession(
        project_id=project_id,
        session_key="session-key",
        status="active",
        messages=[
            {"role": "user", "content": "<<start prompt>>", "timestamp": utcnow().isoformat()},
            _question_msg("What is the goal?"),
        ],
    )
    session: Any = _FakeSession(first_value=onboarding)
    project = SimpleNamespace(id=project_id, name="Roadmap", description="Build v1")
    agent = SimpleNamespace(id=uuid4())

    monkeypatch.setattr(
        onboarding_service.OpenClawAuthorizationPolicy,
        "require_gateway_scoped_actor",
        lambda *, actor_agent: None,
    )
    monkeypatch.setattr(
        onboarding_service.OpenClawAuthorizationPolicy,
        "require_gateway_main_actor_binding",
        lambda *, actor_agent, gateway, gateway_session_key: None,
    )
    async def _fake_get_gateway(_session: object, _project: object) -> None:
        return None

    monkeypatch.setattr(
        onboarding_service,
        "get_gateway_for_project",
        _fake_get_gateway,
    )

    svc = ProjectOnboardingService(session)
    payload = ProjectOnboardingAgentQuestion(
        question="What is your timezone?",
        options=[{"id": "1", "label": "UTC"}],
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await svc.agent_onboarding_update(
            project=project,
            agent=agent,
            payload=payload,
        )

    assert exc_info.value.status_code == 409
    assert "still awaiting" in exc_info.value.detail
