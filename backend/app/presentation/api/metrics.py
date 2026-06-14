"""Dashboard metric aggregation endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.application.use_cases.metrics.service import MetricsService
from app.application.use_cases.organizations.service import OrganizationContext
from app.infrastructure.database.engine import get_session
from app.presentation.api.deps import require_org_member
from app.presentation.schemas.metrics import (
    DashboardMetrics,
    DashboardRangeKey,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])

RANGE_QUERY = Query(default="24h")
PROJECT_ID_QUERY = Query(default=None)
SESSION_DEP = Depends(get_session)
ORG_MEMBER_DEP = Depends(require_org_member)
_RUNTIME_TYPE_REFERENCES = (UUID, AsyncSession)


@router.get("/dashboard", response_model=DashboardMetrics)
async def dashboard_metrics(
    range_key: DashboardRangeKey = RANGE_QUERY,
    project_id: UUID | None = PROJECT_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> DashboardMetrics:
    """Return dashboard KPIs and time-series data for accessible projects."""
    svc = MetricsService(session)
    return await svc.dashboard_metrics(
        range_key=range_key,
        project_id=project_id,
        ctx=ctx,
    )
