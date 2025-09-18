## Enterprise Log Analyzer - Backend

### Getting Started

Requirements:

- Python 3.12+
- Poetry
- Docker + Docker Compose

#### 1) Environment variables

Create `./.env` in `enterprise-log-analyzer-be/` with one of the following setups:

Local (run FastAPI on your host):

```bash
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql+asyncpg://fastapi:fastapi@localhost:5432/fastapi

# Logging (optional)
LOG_LEVEL=INFO
SQLALCHEMY_LOG_LEVEL=WARNING
UVICORN_ACCESS_LOG=false

# Start in-process producer / enricher (optional)
# ENABLE_PRODUCER=1
# ENABLE_ENRICHER=1

# OpenAI (LLM + embeddings). IMPORTANT: use a Project API key.
# If your OpenAI account uses Projects, a user key will fail with 401
# (error code not_authorized_invalid_key_type). Create a Project API key
# and set the project/org identifiers if required by your setup.
OPENAI_API_KEY=sk-proj-...
# Optional but recommended when Projects are enabled:
# OPENAI_ORG_ID=org_...
# OPENAI_PROJECT=proj_...
```

Docker Compose network (when the API runs inside the `app` service):

```bash
REDIS_URL=redis://redis:6379/0
DATABASE_URL=postgresql+asyncpg://fastapi:fastapi@postgres:5432/fastapi

# Logging (optional)
LOG_LEVEL=INFO
SQLALCHEMY_LOG_LEVEL=WARNING
UVICORN_ACCESS_LOG=false

# Start in-process producer/enricher inside the API container (optional)
# ENABLE_PRODUCER=1
# ENABLE_ENRICHER=1

# OpenAI (LLM + embeddings)
OPENAI_API_KEY=sk-proj-...
# Optional when Projects are enabled:
# OPENAI_ORG_ID=org_...
# OPENAI_PROJECT=proj_...
```

Notes:

- `postgres` and `redis` hostnames work only inside Docker Compose. On your host, use `localhost`.
- On startup, the app ensures all SQLAlchemy tables exist (bootstraps the schema). You can later switch to Alembic migrations.

#### 2) Run with Docker Compose

From `enterprise-log-analyzer-be/`:

```bash
docker compose up -d
```

Services:

- `postgres`: Postgres 16 (exposes 5432)
- `redis`: Redis 7 (exposes 6379)
- `app`: FastAPI `http://localhost:8000` (reload)
- `producer` (optional): standalone log producer that tails files under `data/`

Use one producer path:

- In-process producer: set `ENABLE_PRODUCER=1` for the `app` service (or see launcher below)
- Or run the separate `producer` service (already in `docker-compose.yml`)

#### 3) Run locally with Poetry

Install dependencies:

```bash
poetry install
```

Start the API (no producer):

```bash
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Start the API with producer (env flag):

```bash
set ENABLE_PRODUCER=1
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Start via the launcher with a CLI flag:

```bash
poetry run python -m app.run --with-producer --host 0.0.0.0 --port 8000 --reload
```

#### Logging

Console output is concise and readable. Control verbosity via env:

- `LOG_LEVEL` (default `INFO`)
- `SQLALCHEMY_LOG_LEVEL` (default `WARNING`)
- `UVICORN_ACCESS_LOG` (`false` by default to reduce noise)

Each HTTP log includes a `rid` (request id) for correlation.

#### Database schema

On startup, the app runs a safe bootstrap to create tables from SQLAlchemy models. If you prefer Alembic, add migration scripts and switch the startup hook to run `alembic upgrade head`.

#### Troubleshooting

- `[Errno 11001] getaddrinfo failed` or `Error 11001 connecting to redis:6379`:
  - You are running the API on your host but using Docker service names (`redis`, `postgres`). Use `localhost` in `.env` for local runs.
  - For Compose, ensure all services are up: `docker compose ps`.

#### Embeddings provider

Configure which embedding backend to use via env:

```bash
# Options: sentence-transformers | openai | ollama
EMBEDDING_PROVIDER=ollama

# If provider=sentence-transformers
EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2

# If provider=openai (use a Project API key)
OPENAI_API_KEY=sk-proj-...
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
# Optional when Projects are enabled
# OPENAI_ORG_ID=org_...
# OPENAI_PROJECT=proj_...

#### LLM provider (inference/classification)

Configure which LLM backend to use for classification and hypothesis generation:

```bash
# Options: openai | ollama (default: openai)
LLM_PROVIDER=ollama

# If provider=openai (use a Project API key)
OPENAI_API_KEY=sk-proj-...
# Optional when Projects are enabled
# OPENAI_ORG_ID=org_...
# OPENAI_PROJECT=proj_...

# If provider=ollama
OLLAMA_BASE_URL=http://localhost:11434
# Choose an installed chat-capable model
OLLAMA_CHAT_MODEL=mistral
```

When `LLM_PROVIDER=ollama`, the system will use the same Ollama endpoint for both embeddings (if `EMBEDDING_PROVIDER=ollama`) and LLM chat inference.

# If provider=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
```

Notes:

- For Ollama, point `OLLAMA_BASE_URL` to your deployment (e.g., `http://ollama.mydomain:11434`).
- The service calls Ollama’s `POST /api/embeddings` per text and stores vectors in Chroma as before.

### Windows log dataset (27 GB)
Download the large Windows logs archive from Zenodo:

- Direct link: `https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1`

Example commands to fetch and extract locally into the `data/` folder:

```bash
# Create data directory if missing
mkdir -p data

# Using curl
curl -L -o data/Windows.tar.gz "https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1"

# Or using wget
wget -O data/Windows.tar.gz "https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1"

# Verify and extract (may take a while; archive is ~27 GB)
ls -lh data/Windows.tar.gz
tar -xzvf data/Windows.tar.gz -C data
```

If you prefer streaming extraction to save disk space, you can do:

```bash
curl -L "https://zenodo.org/records/8196385/files/Windows.tar.gz?download=1" | tar -xz -C data
```

Note: Ensure you have enough free disk space (>= 60 GB recommended) before download and extraction.
