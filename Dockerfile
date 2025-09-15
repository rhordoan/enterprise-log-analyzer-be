# syntax=docker/dockerfile:1

###############################################
# Builder image                                #
###############################################
FROM python:3.11-slim AS builder

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
ENV POETRY_VERSION="1.7.1" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PIP_NO_CACHE_DIR=1

RUN pip install "poetry==${POETRY_VERSION}"

# Set working directory
WORKDIR /app

# Copy only dependency declarations first for efficient layer caching
COPY pyproject.toml poetry.lock* /app/

# Install dependencies (excluding dev dependencies to keep image slim)
RUN poetry install --no-root --without dev

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

# Copy installed packages from builder stage
COPY --from=builder /usr/local/lib/python* /usr/local/lib/python*
# Copy project source
COPY --from=builder /app /app

# Expose port that the service will listen on
EXPOSE ${PORT}

# Start the application using Uvicorn
CMD ["uvicorn", "${APP_MODULE}", "--host", "${HOST}", "--port", "${PORT}", "--workers", "2"]
