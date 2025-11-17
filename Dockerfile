# syntax=docker/dockerfile:1

###############################################
# Builder image                                #
###############################################
FROM python:3.11-slim AS builder

ARG DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc libgomp1 ca-certificates \
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
    # HuggingFace / Transformers cache paths set to a writable location inside the container
    HF_HOME="/tmp/hf_cache" \
    TRANSFORMERS_CACHE="/tmp/hf_cache" \
    TORCH_HOME="/tmp/torch_cache" \
    TOKENIZERS_PARALLELISM=false \
    HF_HUB_DISABLE_TELEMETRY=1 \
    APP_MODULE="app.main:app" \
    HOST="0.0.0.0" \
    PORT="8000"

# Install runtime libs needed by Torch/Transformers and HTTPS
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Defaults for built-in mock API (accessible via localhost inside the container)
ENV MOCK_HOST="127.0.0.1" \
    MOCK_PORT="8085"

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

# Add entrypoint script to start main app and mock API in parallel
COPY docker/start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# Start both services (main API + mock API). Main app remains reachable at $HOST:$PORT,
# mock API is bound to ${MOCK_HOST}:${MOCK_PORT} so the main app can call http://localhost:${MOCK_PORT}.
CMD ["/usr/local/bin/start.sh"]
