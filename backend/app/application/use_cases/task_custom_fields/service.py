"""TaskCustomFieldService — org-level task custom field definition management.

Extracted from ``app.presentation.api.task_custom_fields``.  Owns project-id
validation, definition lookup, binding reconciliation, and CRUD persistence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app.shared.time import utcnow
from app.infrastructure.models.projects import Project
from app.infrastructure.models.task_custom_fields import (
    ProjectTaskCustomField,
    TaskCustomFieldDefinition,
    TaskCustomFieldValue,
)
from app.presentation.schemas.task_custom_fields import (
    TaskCustomFieldDefinitionRead,
    validate_custom_field_definition,
)

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.application.use_cases.organizations.service import OrganizationContext
    from app.presentation.schemas.task_custom_fields import (
        TaskCustomFieldDefinitionCreate,
        TaskCustomFieldDefinitionUpdate,
    )


class TaskCustomFieldService:
    """Per-request facade for organization custom field definitions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- public API ----------------------------------------------------------

    async def list_definitions(
        self, *, ctx: OrganizationContext,
    ) -> list[TaskCustomFieldDefinitionRead]:
        """List custom field definitions for the authenticated organization."""
        definitions = list(
            await self.session.exec(
                select(TaskCustomFieldDefinition)
                .where(col(TaskCustomFieldDefinition.organization_id) == ctx.organization.id)
                .order_by(func.lower(col(TaskCustomFieldDefinition.label)).asc()),
            ),
        )
        project_ids_by_definition_id = await self._project_ids_by_definition_id(
            definition_ids=[definition.id for definition in definitions],
        )
        return [
            self._to_definition_read_payload(
                definition=definition,
                project_ids=project_ids_by_definition_id.get(definition.id, []),
            )
            for definition in definitions
        ]

    async def create_definition(
        self, *, ctx: OrganizationContext, payload: TaskCustomFieldDefinitionCreate,
    ) -> TaskCustomFieldDefinitionRead:
        """Create an organization-level task custom field definition."""
        project_ids = await self._validated_project_ids_for_org(
            ctx=ctx, project_ids=payload.project_ids,
        )
        try:
            validate_custom_field_definition(
                field_type=payload.field_type,
                validation_regex=payload.validation_regex,
                default_value=payload.default_value,
            )
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(err),
            ) from err
        definition = TaskCustomFieldDefinition(
            organization_id=ctx.organization.id,
            field_key=payload.field_key,
            label=payload.label or payload.field_key,
            field_type=payload.field_type,
            ui_visibility=payload.ui_visibility,
            validation_regex=payload.validation_regex,
            description=payload.description,
            required=payload.required,
            default_value=payload.default_value,
        )
        self.session.add(definition)
        await self.session.flush()
        for project_id in project_ids:
            self.session.add(
                ProjectTaskCustomField(
                    project_id=project_id,
                    task_custom_field_definition_id=definition.id,
                ),
            )
        try:
            await self.session.commit()
        except IntegrityError as err:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Field key already exists in this organization.",
            ) from err

        await self.session.refresh(definition)
        return self._to_definition_read_payload(definition=definition, project_ids=project_ids)

    async def update_definition(
        self,
        *,
        ctx: OrganizationContext,
        definition_id: UUID,
        payload: TaskCustomFieldDefinitionUpdate,
    ) -> TaskCustomFieldDefinitionRead:
        """Update an organization-level task custom field definition."""
        definition = await self._get_org_definition(ctx=ctx, definition_id=definition_id)
        updates = payload.model_dump(exclude_unset=True)
        project_ids = updates.pop("project_ids", None)
        validated_project_ids: list[UUID] | None = None
        if project_ids is not None:
            validated_project_ids = await self._validated_project_ids_for_org(
                ctx=ctx, project_ids=project_ids,
            )
        next_field_type = updates.get("field_type", definition.field_type)
        next_validation_regex = (
            updates["validation_regex"]
            if "validation_regex" in updates
            else definition.validation_regex
        )
        next_default_value = (
            updates["default_value"] if "default_value" in updates else definition.default_value
        )
        try:
            validate_custom_field_definition(
                field_type=next_field_type,
                validation_regex=next_validation_regex,
                default_value=next_default_value,
            )
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(err),
            ) from err
        for key, value in updates.items():
            setattr(definition, key, value)
        if validated_project_ids is not None:
            bindings = list(
                await self.session.exec(
                    select(ProjectTaskCustomField).where(
                        col(ProjectTaskCustomField.task_custom_field_definition_id) == definition.id,
                    ),
                ),
            )
            current_project_ids = {binding.project_id for binding in bindings}
            target_project_ids = set(validated_project_ids)
            for binding in bindings:
                if binding.project_id not in target_project_ids:
                    await self.session.delete(binding)
            for project_id in validated_project_ids:
                if project_id in current_project_ids:
                    continue
                self.session.add(
                    ProjectTaskCustomField(
                        project_id=project_id,
                        task_custom_field_definition_id=definition.id,
                    ),
                )
        definition.updated_at = utcnow()
        self.session.add(definition)

        try:
            await self.session.commit()
        except IntegrityError as err:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Field key already exists in this organization.",
            ) from err

        await self.session.refresh(definition)
        if validated_project_ids is None:
            result_project_ids = (
                await self._project_ids_by_definition_id(definition_ids=[definition.id])
            ).get(definition.id, [])
        else:
            result_project_ids = validated_project_ids
        return self._to_definition_read_payload(
            definition=definition, project_ids=result_project_ids,
        )

    async def delete_definition(
        self, *, ctx: OrganizationContext, definition_id: UUID,
    ) -> None:
        """Delete an org-level definition when it has no persisted task values."""
        definition = await self._get_org_definition(ctx=ctx, definition_id=definition_id)
        value_ids = (
            await self.session.exec(
                select(col(TaskCustomFieldValue.id)).where(
                    col(TaskCustomFieldValue.task_custom_field_definition_id) == definition.id,
                ),
            )
        ).all()
        if value_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete a custom field definition while task values exist.",
            )

        bindings = list(
            await self.session.exec(
                select(ProjectTaskCustomField).where(
                    col(ProjectTaskCustomField.task_custom_field_definition_id) == definition.id,
                ),
            ),
        )
        for binding in bindings:
            await self.session.delete(binding)
        await self.session.delete(definition)
        await self.session.commit()

    # -- private helpers -----------------------------------------------------

    @staticmethod
    def _to_definition_read_payload(
        *, definition: TaskCustomFieldDefinition, project_ids: list[UUID],
    ) -> TaskCustomFieldDefinitionRead:
        payload = TaskCustomFieldDefinitionRead.model_validate(definition, from_attributes=True)
        payload.project_ids = project_ids
        return payload

    async def _project_ids_by_definition_id(
        self, *, definition_ids: list[UUID],
    ) -> dict[UUID, list[UUID]]:
        if not definition_ids:
            return {}
        rows = (
            await self.session.exec(
                select(
                    col(ProjectTaskCustomField.task_custom_field_definition_id),
                    col(ProjectTaskCustomField.project_id),
                ).where(
                    col(ProjectTaskCustomField.task_custom_field_definition_id).in_(definition_ids),
                ),
            )
        ).all()
        project_ids_by_definition_id: dict[UUID, list[UUID]] = {
            definition_id: [] for definition_id in definition_ids
        }
        for definition_id, project_id in rows:
            project_ids_by_definition_id.setdefault(definition_id, []).append(project_id)
        for definition_id in project_ids_by_definition_id:
            project_ids_by_definition_id[definition_id].sort(key=str)
        return project_ids_by_definition_id

    async def _validated_project_ids_for_org(
        self, *, ctx: OrganizationContext, project_ids: list[UUID],
    ) -> list[UUID]:
        normalized_project_ids = list(dict.fromkeys(project_ids))
        if not normalized_project_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="At least one project must be selected.",
            )
        valid_project_ids = set(
            (
                await self.session.exec(
                    select(col(Project.id)).where(
                        col(Project.organization_id) == ctx.organization.id,
                        col(Project.id).in_(normalized_project_ids),
                    ),
                )
            ).all(),
        )
        missing_project_ids = sorted(
            {
                project_id
                for project_id in normalized_project_ids
                if project_id not in valid_project_ids
            },
            key=str,
        )
        if missing_project_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": "Some selected projects are invalid for this organization.",
                    "invalid_project_ids": [str(value) for value in missing_project_ids],
                },
            )
        return normalized_project_ids

    async def _get_org_definition(
        self, *, ctx: OrganizationContext, definition_id: UUID,
    ) -> TaskCustomFieldDefinition:
        definition = (
            await self.session.exec(
                select(TaskCustomFieldDefinition).where(
                    col(TaskCustomFieldDefinition.id) == definition_id,
                    col(TaskCustomFieldDefinition.organization_id) == ctx.organization.id,
                ),
            )
        ).first()
        if definition is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return definition
