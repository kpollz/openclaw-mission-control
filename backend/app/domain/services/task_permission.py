"""Task permission domain service — pure authorization rules.

Determines what actions a given actor (user, lead agent, agent, admin)
can perform on a task. All methods are pure functions that raise
DomainError on authorization failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from app.domain.exceptions import PermissionDeniedError, ValidationError


class TaskPermissionRules:
    """Pure authorization rules for task operations.

    The caller (application layer) loads the relevant context (is the
    actor a lead? what fields are they trying to update?) and passes
    it to these methods for validation.
    """

    # Fields that lead agents are allowed to set via PATCH
    LEAD_ALLOWED_FIELDS: frozenset[str] = frozenset({
        "assigned_agent_id",
        "status",
        "output",
        "depends_on_task_ids",
        "tag_ids",
        "custom_field_values",
    })

    # Fields that agents (non-lead) can update on their own tasks
    AGENT_ALLOWED_FIELDS: frozenset[str] = frozenset({
        "status",
        "output",
    })

    @staticmethod
    def validate_lead_update(
        *,
        requested_fields: set[str],
    ) -> None:
        """Validate that a lead agent's update request only touches allowed fields.

        Raises:
            PermissionDeniedError: if the lead tries to update disallowed fields
                or include a comment (leads must use the comment endpoint).
        """
        if "comment" in requested_fields:
            raise PermissionDeniedError(
                "Lead comment gate failed: project leads cannot include `comment` "
                "in task PATCH. Use the task comments endpoint instead."
            )
        disallowed = requested_fields - TaskPermissionRules.LEAD_ALLOWED_FIELDS
        if disallowed:
            disallowed_str = ", ".join(sorted(disallowed))
            allowed_str = ", ".join(sorted(TaskPermissionRules.LEAD_ALLOWED_FIELDS))
            raise PermissionDeniedError(
                f"Lead field gate failed: unsupported fields for project leads: "
                f"{disallowed_str}. Allowed fields: {allowed_str}."
            )

    @staticmethod
    def validate_agent_update(
        *,
        requested_fields: set[str],
        agent_is_assigned: bool,
    ) -> None:
        """Validate that a non-lead agent's update request is allowed.

        Non-lead agents can only update status on tasks assigned to them.

        Raises:
            PermissionDeniedError: if the agent is not assigned or tries
                to update disallowed fields.
        """
        if not agent_is_assigned:
            raise PermissionDeniedError(
                "Agent is not assigned to this task."
            )
        disallowed = requested_fields - TaskPermissionRules.AGENT_ALLOWED_FIELDS
        if disallowed:
            raise PermissionDeniedError(
                f"Agent can only update status, not: {', '.join(sorted(disallowed))}."
            )

    @staticmethod
    def resolve_update_permissions(
        *,
        actor_type: Literal["user", "agent"],
        is_project_lead: bool,
        is_org_admin: bool,
        agent_is_assigned: bool,
        requested_fields: set[str],
    ) -> Literal["lead", "agent", "admin", "user"]:
        """Determine which permission path to apply for an update.

        Returns one of: "lead", "agent", "admin", "user"
        indicating which validation rules to apply.

        Priority: lead > agent > admin > user
        """
        if actor_type == "agent" and is_project_lead:
            return "lead"
        if actor_type == "agent":
            return "agent"
        if is_org_admin:
            return "admin"
        return "user"

    @staticmethod
    def lead_requested_fields(
        updates: dict[str, object],
        *,
        comment: object | None = None,
        depends_on_task_ids: object | None = None,
        tag_ids: object | None = None,
        custom_field_values_set: bool = False,
    ) -> set[str]:
        """Compute the set of fields being requested in an update."""
        requested = set(updates)
        if comment is not None:
            requested.add("comment")
        if depends_on_task_ids is not None:
            requested.add("depends_on_task_ids")
        if tag_ids is not None:
            requested.add("tag_ids")
        if custom_field_values_set:
            requested.add("custom_field_values")
        return requested
