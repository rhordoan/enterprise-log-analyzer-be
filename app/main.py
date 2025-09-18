from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os

from app.core.config import settings
from app.core.logging_config import configure_logging, install_request_logging
from app.api.v1.api import api_router
from app.streams.consumer import attach_consumer
from app.streams.issues_aggregator import attach_issues_aggregator
from app.db.init_db import init_db
from app.streams.producer import attach_producer
from app.streams.enricher import attach_enricher
import logging

LOG = logging.getLogger(__name__)

# Configure logging before app initialization
configure_logging()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
)

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

attach_consumer(app)
LOG.info("consumer attachment registered")
attach_issues_aggregator(app)
LOG.info("issues aggregator attachment registered")

# Optionally start the producer when ENABLE_PRODUCER is true
if settings.ENABLE_PRODUCER:
    attach_producer(app)
    LOG.info("producer attachment registered (ENABLE_PRODUCER=%s)", settings.ENABLE_PRODUCER)

# Optionally start the enricher when ENABLE_ENRICHER is true
if settings.ENABLE_ENRICHER:
    attach_enricher(app)
    LOG.info("enricher attachment registered (ENABLE_ENRICHER=%s)", settings.ENABLE_ENRICHER)
# Mount versioned API router
app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Simple health-check endpoint."""
    return {"status": "ok"}
