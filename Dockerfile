# syntax=docker/dockerfile:1

###############################################
# Builder image                                #
###############################################
FROM python:3.11-slim AS builder

ARG DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_VERSION="1.7.1" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PIP_NO_CACHE_DIR=1

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "poetry==${POETRY_VERSION}" poetry-plugin-export

# Create a dedicated virtualenv and use it for installed deps (more reliable copy to runtime)
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Set working directory
WORKDIR /app

# Copy only dependency declarations first for efficient layer caching
COPY pyproject.toml poetry.lock* /app/

# Export and install dependencies into the venv (excluding dev) for portability
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cache/pypoetry \
    poetry lock --no-update \
    && poetry export --without dev -f requirements.txt -o requirements.txt \
    && pip install -r requirements.txt \
    && python -c "import uvicorn, aiofiles"

# Copy application code
COPY . /app

###############################################
# Runtime image                                #
###############################################
FROM python:3.11-slim AS runtime

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    APP_MODULE="app.main:app" \
    HOST="0.0.0.0" \
    PORT="8000"

# Workdir inside runtime image
WORKDIR /app

# Copy virtualenv with all installed dependencies
COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copy project source
COPY --from=builder /app /app

# Expose port that the service will listen on
EXPOSE ${PORT}

# Start the application using Uvicorn (use shell to expand env vars reliably)
CMD ["/bin/sh", "-c", "uvicorn $APP_MODULE --host $HOST --port $PORT --workers 2"]
