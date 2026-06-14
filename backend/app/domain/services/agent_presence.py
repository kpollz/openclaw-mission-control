"""Pure rules for converting agent activity into lifecycle presence."""

from __future__ import annotations

from datetime import datetime, timedelta

ONLINE_HEALTH_ALIASES = frozenset(
    {
        "active",
        "healthy",
        "ok",
        "online",
        "provisioning",
        "ready",
        "updating",
    },
)
PRESENCE_HEALTH_STATUSES = frozenset({"degraded", "offline"})


def normalize_heartbeat_status(
    status_value: str | None,
    *,
    current_status: str | None,
) -> str:
    """Return the lifecycle status implied by a successful heartbeat."""
    if current_status == "deleting":
        return "deleting"

    normalized = (status_value or "").strip().lower()
    if not normalized or normalized in ONLINE_HEALTH_ALIASES:
        return "online"
    if normalized in PRESENCE_HEALTH_STATUSES:
        return normalized
    return normalized


def computed_presence_status(
    *,
    current_status: str | None,
    last_seen_at: datetime | None,
    now: datetime,
    offline_after: timedelta,
) -> str:
    """Return the display status implied by persisted lifecycle and heartbeat state."""
    if current_status == "deleting":
        return "deleting"
    if last_seen_at is None:
        if current_status == "updating":
            return "updating"
        return "provisioning"
    if now - last_seen_at > offline_after:
        return "offline"

    normalized = (current_status or "").strip().lower()
    if not normalized or normalized in ONLINE_HEALTH_ALIASES or normalized == "offline":
        return "online"
    return normalized
