import asyncio
import logging
import threading

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
            LOG.info("Starting prototype improver background thread.")
            loop = asyncio.new_event_loop()

            def _runner():
                asyncio.set_event_loop(loop)
                loop.create_task(_run_forever())
                loop.run_forever()

            thread = threading.Thread(target=_runner, name="prototype-improver-thread", daemon=True)
            thread.start()
            app.state.prototype_improver_loop = loop
            app.state.prototype_improver_thread = thread
        else:
            LOG.info("Prototype improver is disabled.")

    @app.on_event("shutdown")
    async def shutdown_event():
        if settings.ENABLE_PROTOTYPE_IMPROVER:
            LOG.info("Stopping prototype improver background thread.")
            loop = getattr(app.state, "prototype_improver_loop", None)
            thread = getattr(app.state, "prototype_improver_thread", None)
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=5)


