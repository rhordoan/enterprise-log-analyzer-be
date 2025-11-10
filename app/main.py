from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging_config import configure_logging, install_request_logging, set_request_logs_enabled
from app.api.v1.api import api_router
from app.streams.consumer import attach_consumer
from app.streams.issues_aggregator import attach_issues_aggregator
from app.db.init_db import init_db
from app.streams.enricher import attach_enricher
from app.services.prototype_improver import attach_prototype_improver
from app.services.llm_service import llm_healthcheck
from app.streams.producer_manager import attach_producers
from app.streams.automations import attach_automations
from app.streams.cluster_enricher import attach_cluster_enricher
from app.streams.metrics_aggregator import attach_metrics_aggregator
import logging
from app.core.runtime_state import set_shutting_down
import threading

LOG = logging.getLogger(__name__)

# Configure logging before app initialization
configure_logging()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
)

# Initialize request logging toggle from settings
try:
    set_request_logs_enabled(bool(getattr(settings, "REQUEST_LOGS_ENABLED", True)))
except Exception:
    pass

install_request_logging(app)

# CORS (allow all by default; tighten via middleware config if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def _init_db_event():
    await init_db()


def _mask_api_key(value: str | None) -> str:
    if not value:
        return "-"
    v = str(value)
    if len(v) <= 10:
        return v[:2] + "***" + v[-2:]
    return v[:6] + "***" + v[-4:]


@app.on_event("startup")
async def _log_llm_configuration():
    masked_key = _mask_api_key(settings.OPENAI_API_KEY)
    LOG.info(
        "LLM configuration provider=%s openai_model=%s openai_api_key=%s",
        settings.LLM_PROVIDER,
        getattr(settings, "OPENAI_CHAT_MODEL", "-"),
        masked_key,
    )
    # Log .env presence for troubleshooting
    try:
        from pathlib import Path as _Path
        env_here = _Path.cwd() / ".env"
        LOG.info(".env present=%s path=%s", env_here.exists(), str(env_here))
    except Exception:
        pass
    # Log Chroma configuration and absolute storage location
    try:
        mode = getattr(settings, "CHROMA_MODE", "local")
        if str(mode).lower() == "http":
            LOG.info(
                "Chroma configuration mode=%s host=%s port=%s",
                mode,
                getattr(settings, "CHROMA_SERVER_HOST", "localhost"),
                getattr(settings, "CHROMA_SERVER_PORT", 8000),
            )
        else:
            chroma_dir = _Path(getattr(settings, "CHROMA_PERSIST_DIRECTORY", ".chroma")).resolve()
            LOG.info("Chroma configuration mode=%s path=%s", mode, str(chroma_dir))
    except Exception:
        pass
    # Proactive LLM health check (non-blocking to avoid startup hangs)
    def _run_hc():
        try:
            hc = llm_healthcheck()
            if hc.get("ok"):
                LOG.info("LLM healthcheck ok provider=%s model=%s", hc.get("provider"), hc.get("model"))
            else:
                LOG.error("LLM healthcheck failed provider=%s model=%s err=%s", hc.get("provider"), hc.get("model"), hc.get("error"))
        except Exception as exc:
            LOG.error("LLM healthcheck raised error err=%s", exc)

    try:
        threading.Thread(target=_run_hc, name="llm-healthcheck", daemon=True).start()
    except Exception:
        pass

attach_consumer(app)
LOG.info("consumer attachment registered")
attach_issues_aggregator(app)
LOG.info("issues aggregator attachment registered")

# Start modular producers based on DB-configured sources
attach_producers(app)
LOG.info("modular producers attachment registered")

# Optionally start the enricher when ENABLE_ENRICHER is true
if settings.ENABLE_ENRICHER:
    attach_enricher(app)
    LOG.info("enricher attachment registered (ENABLE_ENRICHER=%s)", settings.ENABLE_ENRICHER)

# Optionally start the cluster enricher when ENABLE_CLUSTER_ENRICHER is true
attach_cluster_enricher(app)

attach_prototype_improver(app)

# Optionally start automations when ENABLE_AUTOMATIONS is true
if settings.ENABLE_AUTOMATIONS:
    attach_automations(app)
    LOG.info("automations attachment registered (ENABLE_AUTOMATIONS=%s)", settings.ENABLE_AUTOMATIONS)

# Attach metrics aggregator for cluster observability
attach_metrics_aggregator(app)
LOG.info("metrics aggregator attachment registered (ENABLE_CLUSTER_METRICS=%s)", settings.ENABLE_CLUSTER_METRICS)

# Mount versioned API router
app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Simple health-check endpoint."""
    return {"status": "ok"}


@app.on_event("shutdown")
async def _mark_shutting_down():
    try:
        set_shutting_down(True)
    except Exception:
        pass
