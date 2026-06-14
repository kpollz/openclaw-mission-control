from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.application.use_cases.metrics import service as metrics_service


class _FakeSession:
    def __init__(self, exec_result: list[object]) -> None:
        self._exec_result = exec_result

    async def exec(self, _statement: object) -> list[object]:
        return self._exec_result


@pytest.mark.asyncio
async def test_resolve_dashboard_project_ids_returns_requested_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()

    async def _accessible(*_args: object, **_kwargs: object) -> list[object]:
        return [project_id]

    monkeypatch.setattr(
        metrics_service,
        "list_accessible_project_ids",
        _accessible,
    )
    ctx = SimpleNamespace(member=SimpleNamespace(organization_id=uuid4()))

    resolved = await metrics_service._resolve_dashboard_project_ids(
        _FakeSession([]),
        ctx=ctx,
        project_id=project_id,
    )

    assert resolved == [project_id]


@pytest.mark.asyncio
async def test_resolve_dashboard_project_ids_rejects_inaccessible_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accessible_project_id = uuid4()
    requested_project_id = uuid4()

    async def _accessible(*_args: object, **_kwargs: object) -> list[object]:
        return [accessible_project_id]

    monkeypatch.setattr(
        metrics_service,
        "list_accessible_project_ids",
        _accessible,
    )
    ctx = SimpleNamespace(member=SimpleNamespace(organization_id=uuid4()))

    with pytest.raises(HTTPException) as exc_info:
        await metrics_service._resolve_dashboard_project_ids(
            _FakeSession([]),
            ctx=ctx,
            project_id=requested_project_id,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_dashboard_project_ids_returns_all_when_no_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = uuid4()
    project_b = uuid4()

    async def _accessible(*_args: object, **_kwargs: object) -> list[object]:
        return [project_a, project_b]

    monkeypatch.setattr(
        metrics_service,
        "list_accessible_project_ids",
        _accessible,
    )
    ctx = SimpleNamespace(member=SimpleNamespace(organization_id=uuid4()))

    resolved = await metrics_service._resolve_dashboard_project_ids(
        _FakeSession([]),
        ctx=ctx,
        project_id=None,
    )

    assert resolved == [project_a, project_b]
