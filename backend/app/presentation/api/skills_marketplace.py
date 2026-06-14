"""Skills marketplace and skill pack APIs."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.presentation.api.deps import require_org_admin
from app.infrastructure.database.engine import get_session
from app.application.use_cases.organizations.service import OrganizationContext
from app.application.use_cases.skills.service import PackSkillCandidate, SkillsMarketplaceService
from app.presentation.schemas.skills_marketplace import (
    MarketplaceSkillActionResponse,
    MarketplaceSkillCardRead,
    MarketplaceSkillCreate,
    MarketplaceSkillRead,
    SkillPackCreate,
    SkillPackRead,
    SkillPackSyncResponse,
)
from app.presentation.schemas.common import OkResponse

# Module-level aliases for tests
def _collect_pack_skills_from_repo(
    *,
    repo_dir: Path,
    source_url: str,
    branch: str,
) -> list[PackSkillCandidate]:
    """Thin wrapper so tests can call the instance method without ``self``."""
    svc = SkillsMarketplaceService.__new__(SkillsMarketplaceService)
    return svc.collect_pack_skills_from_repo(
        repo_dir=repo_dir, source_url=source_url, branch=branch,
    )


# Static-method aliases (no ``self`` needed)
_validate_pack_source_url = SkillsMarketplaceService.validate_pack_source_url
_install_instruction = SkillsMarketplaceService.install_instruction
_uninstall_instruction = SkillsMarketplaceService.uninstall_instruction
_collect_pack_skills = SkillsMarketplaceService.collect_pack_skills

router = APIRouter(prefix="/skills", tags=["skills"])
SESSION_DEP = Depends(get_session)
ORG_ADMIN_DEP = Depends(require_org_admin)
GATEWAY_ID_QUERY = Query(...)


@router.get("/marketplace", response_model=list[MarketplaceSkillCardRead])
async def list_marketplace_skills(
    response: Response,
    gateway_id: UUID = GATEWAY_ID_QUERY,
    search: str | None = Query(default=None),
    category: str | None = Query(default=None),
    risk: str | None = Query(default=None),
    pack_id: UUID | None = Query(default=None, alias="pack_id"),
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> list[MarketplaceSkillCardRead]:
    """List marketplace cards for an org and annotate install state for a gateway."""
    svc = SkillsMarketplaceService(session)
    return await svc.list_marketplace_skills(
        organization_id=ctx.organization.id,
        gateway_id=gateway_id,
        response=response,
        search=search,
        category=category,
        risk=risk,
        pack_id=pack_id,
        limit=limit,
        offset=offset,
        ctx=ctx,
    )


@router.post("/marketplace", response_model=MarketplaceSkillRead)
async def create_marketplace_skill(
    payload: MarketplaceSkillCreate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> MarketplaceSkillRead:
    """Register or update a direct marketplace skill URL in the catalog."""
    svc = SkillsMarketplaceService(session)
    return await svc.create_marketplace_skill(
        organization_id=ctx.organization.id, payload=payload,
    )


@router.delete("/marketplace/{skill_id}", response_model=OkResponse)
async def delete_marketplace_skill(
    skill_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OkResponse:
    """Delete a marketplace catalog entry and any install records that reference it."""
    svc = SkillsMarketplaceService(session)
    return await svc.delete_marketplace_skill(
        organization_id=ctx.organization.id, skill_id=skill_id, ctx=ctx,
    )


@router.post(
    "/marketplace/{skill_id}/install",
    response_model=MarketplaceSkillActionResponse,
)
async def install_marketplace_skill(
    skill_id: UUID,
    gateway_id: UUID = GATEWAY_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> MarketplaceSkillActionResponse:
    """Install a marketplace skill by dispatching instructions to the gateway agent."""
    svc = SkillsMarketplaceService(session)
    return await svc.install_skill(ctx=ctx, skill_id=skill_id, gateway_id=gateway_id)


@router.post(
    "/marketplace/{skill_id}/uninstall",
    response_model=MarketplaceSkillActionResponse,
)
async def uninstall_marketplace_skill(
    skill_id: UUID,
    gateway_id: UUID = GATEWAY_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> MarketplaceSkillActionResponse:
    """Uninstall a marketplace skill by dispatching instructions to the gateway agent."""
    svc = SkillsMarketplaceService(session)
    return await svc.uninstall_skill(ctx=ctx, skill_id=skill_id, gateway_id=gateway_id)


@router.get("/packs", response_model=list[SkillPackRead])
async def list_skill_packs(
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> list[SkillPackRead]:
    """List skill packs configured for the organization."""
    svc = SkillsMarketplaceService(session)
    return await svc.list_skill_packs(organization_id=ctx.organization.id)


@router.get("/packs/{pack_id}", response_model=SkillPackRead)
async def get_skill_pack(
    pack_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> SkillPackRead:
    """Get one skill pack by ID."""
    svc = SkillsMarketplaceService(session)
    return await svc.get_skill_pack(
        organization_id=ctx.organization.id, pack_id=pack_id, ctx=ctx,
    )


@router.post("/packs", response_model=SkillPackRead)
async def create_skill_pack(
    payload: SkillPackCreate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> SkillPackRead:
    """Register a new skill pack source URL."""
    svc = SkillsMarketplaceService(session)
    return await svc.create_skill_pack(ctx=ctx, payload=payload)


@router.patch("/packs/{pack_id}", response_model=SkillPackRead)
async def update_skill_pack(
    pack_id: UUID,
    payload: SkillPackCreate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> SkillPackRead:
    """Update a skill pack URL and metadata."""
    svc = SkillsMarketplaceService(session)
    return await svc.update_skill_pack(ctx=ctx, pack_id=pack_id, payload=payload)


@router.delete("/packs/{pack_id}", response_model=OkResponse)
async def delete_skill_pack(
    pack_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OkResponse:
    """Delete one pack source from the organization."""
    svc = SkillsMarketplaceService(session)
    return await svc.delete_skill_pack(ctx=ctx, pack_id=pack_id)


@router.post("/packs/{pack_id}/sync", response_model=SkillPackSyncResponse)
async def sync_skill_pack(
    pack_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> SkillPackSyncResponse:
    """Clone a pack repository and upsert discovered skills from `skills/**/SKILL.md`."""
    svc = SkillsMarketplaceService(session)
    return await svc.sync_skill_pack(ctx=ctx, pack_id=pack_id)
