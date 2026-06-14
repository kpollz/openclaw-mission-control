"""Task custom field models and project binding helpers."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Column, UniqueConstraint
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.tenancy import TenantScoped

RUNTIME_ANNOTATION_TYPES = (datetime,)


class TaskCustomFieldDefinition(TenantScoped, table=True):
    """Reusable custom field definition for task metadata."""

    __tablename__ = "task_custom_field_definitions"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "field_key",
            name="uq_task_custom_field_definitions_org_id_field_key",
        ),
        CheckConstraint(
            "field_type IN ('text','text_long','integer','decimal','boolean','date','date_time','url','json')",
            name="ck_tcf_def_field_type",
        ),
        CheckConstraint(
            "ui_visibility IN ('always','if_set','hidden')",
            name="ck_tcf_def_ui_visibility",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    field_key: str = Field(index=True)
    label: str
    field_type: str = Field(default="text")
    ui_visibility: str = Field(default="always")
    validation_regex: str | None = None
    description: str | None = None
    required: bool = Field(default=False)
    required_for_done: bool = Field(default=False)
    default_value: object | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProjectTaskCustomField(TenantScoped, table=True):
    """Project-level binding of a custom field definition."""

    __tablename__ = "project_task_custom_fields"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "task_custom_field_definition_id",
            name="uq_proj_task_cf_proj_id_cf_def_id",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    task_custom_field_definition_id: UUID = Field(
        foreign_key="task_custom_field_definitions.id",
        index=True,
    )
    created_at: datetime = Field(default_factory=utcnow)


class TaskCustomFieldValue(TenantScoped, table=True):
    """Stored task-level values for bound custom fields."""

    __tablename__ = "task_custom_field_values"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_custom_field_definition_id",
            name="uq_task_cf_vals_task_id_cf_def_id",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    task_id: UUID = Field(foreign_key="tasks.id", index=True)
    task_custom_field_definition_id: UUID = Field(
        foreign_key="task_custom_field_definitions.id",
        index=True,
    )
    value: object | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
