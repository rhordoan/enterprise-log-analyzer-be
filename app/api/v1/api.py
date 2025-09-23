from fastapi import APIRouter

from app.api.v1.endpoints import items
from app.api.v1.endpoints import alerts
from app.api.v1.endpoints import incidents
from app.api.v1.endpoints import health

api_router = APIRouter()
api_router.include_router(items.router, prefix="/items", tags=["items"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(incidents.router, prefix="/incidents", tags=["incidents"])
api_router.include_router(health.router, prefix="/health", tags=["health"])
