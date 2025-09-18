import logging
import logging.config
import os
import time
import uuid
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class RequestIdFilter(logging.Filter):
    """Attach request_id to log records if present in record.extra, else '-'."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if not hasattr(record, "request_id"):
            record.request_id = "-"  # type: ignore[attr-defined]
        return True


class SimpleConsoleFormatter(logging.Formatter):
    """Minimal, readable console format for terminal use."""

    default_time_format = "%Y-%m-%d %H:%M:%S"
    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        record.message = record.getMessage()
        asctime = self.formatTime(record, self.datefmt)
        rid = getattr(record, "request_id", "-")
        return f"{asctime} | {record.levelname} | {record.name} | {record.message} | rid={rid}"


def configure_logging() -> None:
    """Configure application-wide logging from environment variables.

    ENV:
    - LOG_LEVEL (default INFO)
    - SQLALCHEMY_LOG_LEVEL (default WARNING)
    - UVICORN_ACCESS_LOG (default true)
    """

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    sqlalchemy_level = os.getenv("SQLALCHEMY_LOG_LEVEL", "WARNING").upper()
    uvicorn_access = os.getenv("UVICORN_ACCESS_LOG", "false").lower() not in {"0", "false", "no"}

    handler: Dict[str, Any] = {
        "class": "logging.StreamHandler",
        "level": log_level,
        "formatter": "kv",
        "filters": ["request_id"],
    }

    dict_config: Dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": RequestIdFilter},
        },
        "formatters": {
            "simple": {"()": SimpleConsoleFormatter},
        },
        "handlers": {
            "console": {**handler, "formatter": "simple"},
        },
        "loggers": {
            "": {"handlers": ["console"], "level": log_level, "propagate": False},
            "uvicorn": {"handlers": ["console"], "level": log_level, "propagate": False},
            "uvicorn.access": {"handlers": ["console"], "level": ("INFO" if uvicorn_access else "WARNING"), "propagate": False},
            "sqlalchemy.engine": {"handlers": ["console"], "level": sqlalchemy_level, "propagate": False},
            # Quiet noisy libraries
            "httpx": {"handlers": ["console"], "level": "WARNING", "propagate": False},
            "httpcore": {"handlers": ["console"], "level": "WARNING", "propagate": False},
            "chromadb": {"handlers": ["console"], "level": "WARNING", "propagate": False},
            "chromadb.telemetry": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        },
    }

    logging.config.dictConfig(dict_config)


def install_request_logging(app: FastAPI) -> None:
    """Add request/response logging middleware and exception handler."""

    @app.middleware("http")
    async def log_requests(request: Request, call_next):  # type: ignore[override]
        logger = logging.getLogger("http")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        logger.info("HTTP %s %s start", request.method, request.url.path, extra={"request_id": request_id})
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.info("HTTP %s %s error status=500 err=%s", request.method, request.url.path, exc, extra={"request_id": request_id})
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info("HTTP %s %s %s %.2fms", request.method, request.url.path, response.status_code, elapsed_ms, extra={"request_id": request_id})
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[override]
        logger = logging.getLogger("app.errors")
        request_id = request.headers.get("x-request-id") or "-"
        logger.info("unhandled exception path=%s err=%s", request.url.path, exc, extra={"request_id": request_id})
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


