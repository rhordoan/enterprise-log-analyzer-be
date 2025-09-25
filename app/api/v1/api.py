from fastapi import APIRouter

from app.api.v1.endpoints import items
from app.api.v1.endpoints import alerts
from app.api.v1.endpoints import incidents
from app.api.v1.endpoints import health
from app.api.v1.endpoints import sources
from app.api.v1.endpoints import telemetry

api_router = APIRouter()
api_router.include_router(items.router, prefix="/items", tags=["items"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(incidents.router, prefix="/incidents", tags=["incidents"])
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(sources.router, prefix="/sources", tags=["sources"])
api_router.include_router(telemetry.router, prefix="/telemetry", tags=["telemetry"])
