import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI

from app.core.config import get_settings
from scripts.improve_prototypes import improve_prototypes

LOG = logging.getLogger(__name__)
settings = get_settings()


def attach_prototype_improver(app: FastAPI):
    """Attach a periodic task to improve prototypes based on feedback."""

    async def _run_forever():
        backoff = 5.0
        while True:
            try:
                await improve_prototypes()
                # Wait for the configured interval before the next run
                await asyncio.sleep(settings.PROTOTYPE_IMPROVER_INTERVAL_SEC)
                backoff = 5.0  # Reset backoff on success
            except Exception as exc:
                LOG.error(f"Prototype improver crashed: {exc}; restarting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)  # Cap backoff at 5 minutes

    @app.on_event("startup")
    async def startup_event():
        if settings.ENABLE_PROTOTYPE_IMPROVER:
            LOG.info("Starting prototype improver background task.")
            app.state.prototype_improver_task = asyncio.create_task(_run_forever())
        else:
            LOG.info("Prototype improver is disabled.")

    @app.on_event("shutdown")
    async def shutdown_event():
        if settings.ENABLE_PROTOTYPE_IMPROVER:
            LOG.info("Stopping prototype improver background task.")
            task = getattr(app.state, "prototype_improver_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
