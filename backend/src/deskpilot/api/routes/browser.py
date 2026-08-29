"""Authenticated, read-only Browser control-plane projection."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from deskpilot.api.dependencies import get_browser_control_plane
from deskpilot.application.browser_control_plane import BrowserControlPlaneService
from deskpilot.domain.browser_control_plane import BrowserControlPlaneSnapshot

router = APIRouter(prefix="/browser", tags=["browser"])

BrowserControlPlaneDependency = Annotated[
    BrowserControlPlaneService,
    Depends(get_browser_control_plane),
]


@router.get("/control-plane", response_model=BrowserControlPlaneSnapshot)
async def get_browser_control_plane_snapshot(
    response: Response,
    service: BrowserControlPlaneDependency,
) -> BrowserControlPlaneSnapshot:
    snapshot = await service.snapshot()
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = (
        f'"browser-control-plane-v{snapshot.revision}-'
        f'{snapshot.snapshot_digest[:12]}"'
    )
    return snapshot
