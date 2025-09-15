from fastapi import FastAPI

from app.core.config import settings
from app.api.v1.api import api_router
from app.streams.consumer import attach_consumer

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
)

attach_consumer(app)
# Mount versioned API router
app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Simple health-check endpoint."""
    return {"status": "ok"}
