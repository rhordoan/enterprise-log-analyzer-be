from fastapi import APIRouter
from datetime import datetime, timezone

router = APIRouter()


@router.get("/", tags=["health"])
async def health() -> dict[str, object]:
    """Lightweight health endpoint for liveness checks."""
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
    }




