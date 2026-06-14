"""Task lifecycle domain service — pure business rules for status transitions.

This service encapsulates the project-level configuration rules that govern
how tasks transition between statuses. All methods are pure functions that
take configuration values (loaded by the application layer) and raise
DomainError on rule violations.

The presentation layer catches these DomainErrors and maps them to HTTP
responses via the error_mapper.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.domain.exceptions import (
    ApprovalRequiredError,
    ConflictError,
    TaskBlockedError,
    ValidationError,
)


class TaskLifecycleRules:
    """Pure business rules for task status transitions.

    All methods receive pre-fetched configuration values (no I/O).
    The caller (application layer) is responsible for loading these
    values from the database and passing them in.
    """

    @staticmethod
    def validate_done_transition(
        *,
        previous_status: str,
        target_status: str,
        # Project-level configuration flags (loaded by caller)
        require_review_before_done: bool,
        require_approval_for_done: bool,
        block_status_changes_with_pending_approval: bool,
        has_approved_linked_approval: bool,
        has_pending_linked_approval: bool,
        # Dependency blocking
        blocked_by_task_ids: Sequence[UUID],
        # Custom field validation
        missing_required_custom_field_keys: Sequence[str] | None = None,
        status_requested: bool = True,
    ) -> None:
        """Validate all rules for transitioning a task, raising on violation.

        This combines all project-level gating rules into a single validation
        pass. The caller should pre-fetch all configuration and state, then
        call this method to validate.

        Raises:
            TaskBlockedError: task has incomplete dependencies
            ApprovalRequiredError: done requires approved linked approval
            ConflictError: various rule violations (review required, pending approval)
            ValidationError: required custom fields missing
        """
        if not status_requested or previous_status == target_status:
            return

        # 1. Pending approval blocks any status change
        if block_status_changes_with_pending_approval and has_pending_linked_approval:
            raise ConflictError(
                "Task status cannot be changed while a linked approval is pending."
            )

        # Rules below only apply when transitioning TO done
        if target_status != "done" or previous_status == "done":
            return

        # 2. Blocked by dependencies
        if blocked_by_task_ids:
            raise TaskBlockedError(
                message="Task is blocked by incomplete dependencies.",
                blocked_by_task_ids=list(blocked_by_task_ids),
            )

        # 3. Review-before-done rule
        if require_review_before_done and previous_status != "review":
            raise ConflictError(
                "Task can only be marked done from review when the project rule is enabled."
            )

        # 4. Approval-for-done rule
        if require_approval_for_done and not has_approved_linked_approval:
            raise ApprovalRequiredError(
                "Task can only be marked done when a linked approval has been approved."
            )

        # 5. Required custom fields for done
        if missing_required_custom_field_keys:
            raise ValidationError(
                "Task can only be marked done after required output fields are filled."
            )

    @staticmethod
    def requires_comment_for_review(
        *,
        comment_required_for_review: bool,
    ) -> bool:
        """Check whether a comment is required when moving to review status."""
        return comment_required_for_review

    @staticmethod
    def can_transition_to_done_from(
        *,
        previous_status: str,
        require_review_before_done: bool,
    ) -> bool:
        """Pure check: can a task go to done from its current status?"""
        if previous_status == "done":
            return False
        if require_review_before_done and previous_status != "review":
            return False
        return True


class CustomFieldRules:
    """Pure business rules for task custom field validation."""

    @staticmethod
    def find_missing_required_for_done(
        *,
        effective_values: dict[str, object],
        definitions_by_key: dict[str, object],
    ) -> list[str]:
        """Return field keys that are required-for-done but missing/empty.

        `definitions_by_key` values should have `required_for_done: bool`
        attribute. Returns empty list if all required fields are filled.
        """
        missing: list[str] = []
        for key, definition in definitions_by_key.items():
            if not getattr(definition, "required_for_done", False):
                continue
            value = effective_values.get(key)
            if value is None or value == "" or value == []:
                missing.append(key)
        return missing

    @staticmethod
    def validate_field_keys(
        *,
        provided_keys: set[str],
        known_keys: set[str],
    ) -> list[str]:
        """Return keys in provided_keys that are not in known_keys."""
        return sorted(provided_keys - known_keys)
