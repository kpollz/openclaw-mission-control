# ruff: noqa: INP001
"""Schema validation tests for project and onboarding goal requirements."""

from uuid import uuid4

import pytest

from app.presentation.schemas.project_onboarding import ProjectOnboardingConfirm
from app.presentation.schemas.projects import ProjectCreate, ProjectUpdate


def test_goal_project_requires_objective_and_metrics_when_confirmed() -> None:
    """Confirmed goal projects should require objective and success metrics."""
    with pytest.raises(
        ValueError,
        match="Confirmed goal projects require objective and success_metrics",
    ):
        ProjectCreate(
            name="Goal Project",
            slug="goal",
            description="Ship onboarding improvements.",
            gateway_id=uuid4(),
            project_type="goal",
            goal_confirmed=True,
        )

    ProjectCreate(
        name="Goal Project",
        slug="goal",
        description="Ship onboarding improvements.",
        gateway_id=uuid4(),
        project_type="goal",
        goal_confirmed=True,
        objective="Launch onboarding",
        success_metrics={"emails": 3},
    )


def test_goal_project_allows_missing_objective_before_confirmation() -> None:
    """Draft goal projects may omit objective/success_metrics before confirmation."""
    ProjectCreate(
        name="Draft",
        slug="draft",
        description="Iterate on backlog hygiene.",
        gateway_id=uuid4(),
        project_type="goal",
    )


def test_general_project_allows_missing_objective() -> None:
    """General projects should allow missing goal-specific fields."""
    ProjectCreate(
        name="General",
        slug="general",
        description="General coordination project.",
        gateway_id=uuid4(),
        project_type="general",
    )


def test_project_create_requires_description() -> None:
    """Project creation should reject empty descriptions."""
    with pytest.raises(ValueError, match="description is required"):
        ProjectCreate(
            name="Goal Project",
            slug="goal",
            description="  ",
            gateway_id=uuid4(),
            project_type="goal",
        )


def test_project_update_rejects_empty_description_patch() -> None:
    """Patch payloads should reject blank descriptions."""
    with pytest.raises(ValueError, match="description is required"):
        ProjectUpdate(description="   ")


def test_project_rule_toggles_have_expected_defaults() -> None:
    """Projects should default to approval-gated done and optional review gating."""
    created = ProjectCreate(
        name="Ops Project",
        slug="ops-project",
        description="Operations workflow project.",
        gateway_id=uuid4(),
    )
    assert created.require_approval_for_done is True
    assert created.require_review_before_done is False
    assert created.comment_required_for_review is False
    assert created.block_status_changes_with_pending_approval is False
    assert created.only_lead_can_change_status is False
    assert created.max_agents == 1

    updated = ProjectUpdate(
        require_approval_for_done=False,
        require_review_before_done=True,
        comment_required_for_review=True,
        block_status_changes_with_pending_approval=True,
        only_lead_can_change_status=True,
        max_agents=3,
    )
    assert updated.require_approval_for_done is False
    assert updated.require_review_before_done is True
    assert updated.comment_required_for_review is True
    assert updated.block_status_changes_with_pending_approval is True
    assert updated.only_lead_can_change_status is True
    assert updated.max_agents == 3


def test_project_max_agents_must_be_non_negative() -> None:
    """Project max_agents should reject negative values."""
    with pytest.raises(ValueError):
        ProjectCreate(
            name="Ops Project",
            slug="ops-project",
            description="Operations workflow project.",
            gateway_id=uuid4(),
            max_agents=-1,
        )

    with pytest.raises(ValueError):
        ProjectUpdate(max_agents=-1)


def test_onboarding_confirm_requires_goal_fields() -> None:
    """Onboarding confirm should enforce goal fields for goal project types."""
    with pytest.raises(
        ValueError,
        match="Confirmed goal projects require objective and success_metrics",
    ):
        ProjectOnboardingConfirm(project_type="goal")

    with pytest.raises(
        ValueError,
        match="Confirmed goal projects require objective and success_metrics",
    ):
        ProjectOnboardingConfirm(project_type="goal", objective="Ship onboarding")

    with pytest.raises(
        ValueError,
        match="Confirmed goal projects require objective and success_metrics",
    ):
        ProjectOnboardingConfirm(project_type="goal", success_metrics={"emails": 3})

    ProjectOnboardingConfirm(
        project_type="goal",
        objective="Ship onboarding",
        success_metrics={"emails": 3},
    )

    ProjectOnboardingConfirm(project_type="general")
