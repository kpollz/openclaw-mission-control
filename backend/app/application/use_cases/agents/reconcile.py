"""Worker handlers for lifecycle reconciliation tasks."""

from __future__ import annotations

import asyncio

from app.shared.logging import get_logger
from app.shared.time import utcnow
from app.infrastructure.database.engine import async_session_maker
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.gateway.constants import MAX_WAKE_ATTEMPTS_WITHOUT_CHECKIN
from app.application.use_cases.agents.lifecycle import AgentLifecycleOrchestrator
from app.infrastructure.queue.lifecycle_queue import decode_lifecycle_task, defer_lifecycle_reconcile
from app.infrastructure.queue.redis_queue import QueuedTask

logger = get_logger(__name__)
_RECONCILE_TIMEOUT_SECONDS = 60.0


def _has_checked_in_since_wake(agent: Agent) -> bool:
    if agent.last_seen_at is None:
        return False
    if agent.last_wake_sent_at is None:
        return True
    return agent.last_seen_at >= agent.last_wake_sent_at


async def process_lifecycle_queue_task(task: QueuedTask) -> None:
    """Re-run lifecycle provisioning when an agent misses post-provision check-in."""
    payload = decode_lifecycle_task(task)
    now = utcnow()

    async with async_session_maker() as session:
        agent = await Agent.objects.by_id(payload.agent_id).first(session)
        if agent is None:
            logger.info(
                "lifecycle.reconcile.skip_missing_agent",
                extra={"agent_id": str(payload.agent_id)},
            )
            return

        # Ignore stale queue messages after a newer lifecycle generation.
        if agent.lifecycle_generation != payload.generation:
            logger.info(
                "lifecycle.reconcile.skip_stale_generation",
                extra={
                    "agent_id": str(agent.id),
                    "queued_generation": payload.generation,
                    "current_generation": agent.lifecycle_generation,
                },
            )
            return

        if _has_checked_in_since_wake(agent):
            logger.info(
                "lifecycle.reconcile.skip_not_stuck",
                extra={"agent_id": str(agent.id), "status": agent.status},
            )
            return

        deadline = agent.checkin_deadline_at or payload.checkin_deadline_at
        if agent.status == "deleting":
            logger.info(
                "lifecycle.reconcile.skip_deleting",
                extra={"agent_id": str(agent.id)},
            )
            return

        if now < deadline:
            delay = max(0.0, (deadline - now).total_seconds())
            if not defer_lifecycle_reconcile(task, delay_seconds=delay):
                msg = "Failed to defer lifecycle reconcile task"
                raise RuntimeError(msg)
            logger.info(
                "lifecycle.reconcile.deferred",
                extra={"agent_id": str(agent.id), "delay_seconds": delay},
            )
            return

        if agent.wake_attempts >= MAX_WAKE_ATTEMPTS_WITHOUT_CHECKIN:
            agent.status = "offline"
            agent.checkin_deadline_at = None
            agent.last_provision_error = (
                "Agent did not check in after wake; max wake attempts reached"
            )
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()
            logger.warning(
                "lifecycle.reconcile.max_attempts_reached",
                extra={
                    "agent_id": str(agent.id),
                    "wake_attempts": agent.wake_attempts,
                    "max_attempts": MAX_WAKE_ATTEMPTS_WITHOUT_CHECKIN,
                },
            )
            return

        gateway = await Gateway.objects.by_id(agent.gateway_id).first(session)
        if gateway is None:
            logger.warning(
                "lifecycle.reconcile.skip_missing_gateway",
                extra={"agent_id": str(agent.id), "gateway_id": str(agent.gateway_id)},
            )
            return
        project: Project | None = None
        if agent.project_id is not None:
            project = await Project.objects.by_id(agent.project_id).first(session)
            if project is None:
                logger.warning(
                    "lifecycle.reconcile.skip_missing_project",
                    extra={"agent_id": str(agent.id), "project_id": str(agent.project_id)},
                )
                return

        orchestrator = AgentLifecycleOrchestrator(session)

        # Mint a fresh token for this reconcile and let the wakeup message
        # redeliver it to the agent's credential file. We no longer try to
        # recover the previous token from the rendered TOOLS.md (the token no
        # longer lives in any gateway-readable workspace file). Rotating is cheap
        # now — the agent just rewrites mission_control_credential.json on wake.
        await asyncio.wait_for(
            orchestrator.run_lifecycle(
                gateway=gateway,
                agent_id=agent.id,
                project=project,
                user=None,
                action="update",
                auth_token=None,
                force_bootstrap=False,
                reset_session=True,
                wake=True,
                deliver_wakeup=True,
                wakeup_verb="updated",
                clear_confirm_token=True,
                raise_gateway_errors=True,
            ),
            timeout=_RECONCILE_TIMEOUT_SECONDS,
        )
        logger.info(
            "lifecycle.reconcile.retriggered",
            extra={"agent_id": str(agent.id), "generation": payload.generation},
        )
