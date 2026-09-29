# Aeterna RAG API 
https://deepwiki.com/Abiolr/aeterna-RAG

*Aeterna*, Latin for **"eternal"**, is a standalone RAG-based microservice
that scores a file's long-term survivability (0–100): how likely it is to
remain readable and accessible decades from now, based on file-format
openness, adoption, supersession status, metadata richness, and lifecycle
health. It uses PRONOM + Library of Congress format registry data, a vector
store for semantic context, and an LLM (via the Anthropic API) to produce
grounded, citation-backed sub-scores, which are then combined into a final
score deterministically in code.

## Origin

Aeterna is a reimplementation of the scoring engine from **[Domus
Memoriae](https://github.com/Abiolr/domus-memoriae)**, a family digital-archive
platform built for CalgaryHacks 2026. Domus Memoriae's original "Archive
Engine" scored file risk using a Random Forest classifier trained on
synthetic data. Aeterna rebuilds that idea from scratch as a RAG system:
instead of a black-box model producing a number, it retrieves real
preservation literature (PRONOM registry data, Library of Congress
sustainability documentation) and has an LLM reason over that evidence to
produce explainable, evidence-cited sub-scores.

## The scoring framework

The survivability score is a weighted combination of five sub-scores, each
in the range 0.0–1.0 (1.0 = maximally favorable/safe):

| Term | Weight | What it measures |
|------|--------|-------------------|
| **Openness** | 0.25 | Is the format an open, disclosed standard, or closed/proprietary? Based on the `disclosure` and `licensing_and_patents` fields from the Library of Congress sustainability data — i.e. whether the format's specification is publicly documented, and whether it's encumbered by patents or restrictive licensing. |
| **Adoption** | 0.25 | How widely is the format actually used and supported? Based on the LoC `adoption` field, mapped from qualitative language (e.g. "industry standard" → high, "limited support" → low). |
| **Non-supersession** | 0.20 | Has this format been replaced by a newer version? Based directly on the `is_superseded` flag from PRONOM registry data — structured fact, not LLM inference. |
| **Metadata completeness** | 0.15 | How much provenance/content metadata (title, author, description, authenticity/C2PA claims) does the *uploaded file itself* carry, versus just opaque technical container fields? |
| **Age / lifecycle health** | 0.15 | Is the format actively maintained, or aged past its support horizon? Requires both a `release_date` and either a `withdrawn_date` or explicit lifecycle language — PRONOM populates these for only a small minority of formats, so this term is frequently `null`. |

### Who computes what

The **LLM only assigns the five sub-scores** (each with cited evidence), lists
`missing_inputs`, and writes the explanation and markdown summary. It is
explicitly instructed to use only the retrieved CONTEXT — never outside
knowledge — and to return `null` for any term it can't ground in the data.

The **final `survivability_score` is computed deterministically in Python**
(`compute_survivability_score` in `services/llm_inference.py`): each non-null
sub-score is multiplied by its weight, and the sum is divided by the total
weight of the non-null terms (i.e. weights are renormalized over the terms
that have evidence). If the model emits a `survivability_score` anyway, it is
discarded so the code-computed value is the single source of truth. If every
term is `null`, the score is `null`.

## How it works (pipeline walkthrough)

Here's what happens, step by step, from the moment a file is uploaded to
the moment a score comes back:

1. **Authenticate** — `POST /score` requires an `X-API-Key` header. The key
   is SHA-256 hashed and looked up in the PostgreSQL `api_keys` table
   (`services/auth.py`). Missing or invalid keys get a `401`.
2. **Rate limit** — a fixed-window Redis counter keyed by the hashed API key
   (default 60 requests / 60 s) is checked. Over the limit → `429` with a
   `Retry-After` header.
3. **Upload** — the file arrives over `multipart/form-data` (FastAPI
   `UploadFile`). A random filename is generated for it (preserving the
   original extension), and it is saved under that name to a temp folder —
   this avoids trusting the client-supplied filename and avoids collisions
   between concurrent uploads.
4. **Check the cache** — `services/cache.py` hashes the uploaded file
   (SHA-256) and checks Redis for a previously computed result under that
   hash. On a hit, the cached `{json, summary}` result is returned
   immediately and the rest of the pipeline is skipped.
5. **Extract metadata** — `services/file_extraction.py` runs the file
   through ExifTool to pull out whatever embedded metadata exists (title,
   author, timestamps, technical details), and figures out the file's
   extension.
6. **Look up the format** — `services/build_lookup_db.py` takes that
   extension and queries the **PostgreSQL** lookup tables, built from PRONOM
   (the UK National Archives' format registry) and Library of Congress
   format data. This returns hard facts about the format: whether it's
   open or proprietary, whether it's been superseded, and any known
   release/withdrawal dates.
7. **Find similar formats** — `services/vector_db_setup.py` takes a short
   natural-language question ("is .png a strong, sustainable file
   format?") and searches a vector database (ChromaDB) of Library of
   Congress sustainability write-ups, pulling back documents about
   comparable formats. This gives the LLM extra context to reason with —
   it's not used to identify the file itself, just to add comparative
   grounding.
8. **Assemble the context** — `services/data_pipeline.py` bundles the
   ExifTool metadata, the PostgreSQL lookup results, and the vector search
   results into one package.
9. **Build the prompt** — `services/system_prompt.py` takes that package
   and turns it into a detailed instruction set for the LLM: the five
   sub-scores and how to assign each one, and strict rules against
   inventing information that isn't in the retrieved data.
10. **Score it** — `services/llm_inference.py` sends that prompt to the
    Anthropic API (`claude-haiku-4-5-20251001`), which returns a JSON
    object with the five sub-scores (each backed by cited evidence),
    `missing_inputs`, an explanation, and a markdown summary. The code then
    computes the final `survivability_score` from those sub-scores (see
    [Who computes what](#who-computes-what)). The token usage of the call is
    recorded for the metrics (see [Observability](#observability--metrics)).
11. **Cache and return the result** — `app.py` parses the response into
    JSON plus a markdown summary, writes it to Redis (keyed by file hash,
    7-day TTL) via `cache_score`, and sends it back to the caller. The temp
    upload is always deleted afterward. If anything fails along the way, the
    client gets a generic error message — the real error (stack trace, raw
    LLM output, etc.) is only ever logged server-side, never exposed over
    the API.

Throughout all of this, an HTTP middleware in `app.py` times every request and
the pipeline records cache hits/misses, rate-limit rejections, inference time,
failures, and LLM token usage as Prometheus metrics.

## Tech stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Language | Python 3.11+ | Docker image runs Python 3.13; CI runs 3.11 |
| Web framework | FastAPI (Starlette) | `uvicorn --reload` locally; `uvicorn` with 2 worker processes in Docker. Interactive API docs are served at `/docs` |
| LLM | Anthropic API (`claude-haiku-4-5-20251001`) | Called via the official `anthropic` Python SDK in `services/llm_inference.py` |
| Vector store | ChromaDB (persistent client) | Seeded with Library of Congress format-sustainability documents; stored on local disk in the container (`db/aeterna_vector_db/`) |
| Embeddings | `all-MiniLM-L6-v2` (sentence-transformers) | Runs locally (CPU-only torch) — embeds both the seeded LoC documents and incoming queries |
| Structured lookup DB | **PostgreSQL** (raw SQL via `psycopg`, no ORM) | Holds the PRONOM/LoC format tables *and* the `api_keys` table. **Production runs on AWS RDS**; local dev uses the `postgres:17-alpine` container. Dataset is static/write-once, so no ORM/migrations layer is used |
| Auth | API keys (`atna_…`) | Generated with `secrets.token_hex`; only SHA-256 hashes are stored in PostgreSQL |
| Cache + rate limiting | Redis | Caches `/score` results by SHA-256 file hash (7-day TTL) and backs the fixed-window rate limiters (`services/cache.py`) |
| File metadata extraction | ExifTool, via `pyexiftool` | Requires the ExifTool system binary, not just the Python wrapper |
| Config | `python-dotenv` | Loads `ANTHROPIC_API_KEY`, `REDIS_URL`, `POSTGRES_URL`, etc. from `.env` |
| Metrics instrumentation | `prometheus-client` | Counters/histograms defined in `services/metrics.py`, exposed at `GET /metrics`. Runs in multiprocess mode in Docker so both uvicorn workers are aggregated |
| Metrics storage | Prometheus (`prom/prometheus`) | Scrapes the API every 15 s over the internal Docker network; 15-day retention. Bound to `127.0.0.1:9090` on the host |
| Dashboards | Grafana (`grafana/grafana`) | Prometheus datasource is provisioned from `grafana/provisioning/datasources/`. Bound to `127.0.0.1:3001` on the host |
| Containerization | Docker + Docker Compose | Multi-stage `dockerfile`; `docker-compose.yml` (prod) wires up the API, Redis, Prometheus, Grafana, Nginx, and Certbot; `docker-compose.dev.yml` adds a local Postgres |
| Reverse proxy / TLS | Nginx + Certbot | Nginx terminates HTTPS and proxies to the API container; Certbot manages Let's Encrypt certificate issuance/renewal |
| Hosting | AWS EC2 (Elastic IP) | Long-lived public IP for the instance |
| Database hosting | AWS RDS (PostgreSQL) | Production lookup + API-key database. Only reachable from the EC2 instance (not publicly accessible) |
| Backup storage | AWS S3 | Manually stored copies of the source JSON, vector DB, and `raw_xml/`. The application never reads from or writes to S3 — see [Data storage & backups](#data-storage--backups) |
| DNS | DuckDNS | Public hostname: `aeterna-api.duckdns.org` |
| Image registry | Docker Hub | `abiolar/aeterna-rag-api` |
| CI/CD | GitHub Actions | Runs tests on every push/PR; on a green run, builds and pushes the image and deploys to EC2 over SSH (see [Deployment](#deployment-production) below) |
| Uptime monitoring | UptimeRobot | Polls the public `/health` endpoint; a `503` (degraded Postgres/Redis) or no response registers as down (see [Monitoring](#monitoring)) |
| Data sources | PRONOM registry, Library of Congress Sustainability of Digital Formats | See `data/` and `raw_xml/` |

**Deliberately not used:** LangChain — the RAG pipeline (retrieval, prompt
assembly, LLM call) is hand-built on top of the ChromaDB and Anthropic SDKs
directly, since abstracting away those mechanics would defeat the point of
using this project to learn RAG from primitives. `pgvector` was also
considered and dropped: PostgreSQL is used for the structured format and
auth tables, but semantic search stays in ChromaDB.

## Data storage & backups

Aeterna keeps its data in a few places, each with a specific job:

| Data | Where it lives (production) | Backup |
|------|------------------------------|--------|
| **Format lookup + API keys** (PostgreSQL) | **AWS RDS**, reachable only from the EC2 instance | None |
| **Source JSON exports** (`data/*.json` — PRONOM/LoC) | Baked into the image / repo working tree; used to (re)build the Postgres tables and seed the vector store | **AWS S3** |
| **Vector DB** (`db/aeterna_vector_db/`, ChromaDB) | Local disk inside the API container | **AWS S3** |
| **Raw source XML** (`raw_xml/`) | Not in the image (excluded via `.dockerignore`) | **AWS S3** |
| **Score cache** (Redis) | Redis container, persisted to the `redis-data` volume | None (disposable; safe to lose) |
| **Metrics history** (Prometheus) | `prometheus-data` volume, 15-day retention | None (disposable) |
| **Grafana state** (dashboards, users) | `grafana-data` volume | None — dashboards built in the UI are lost if the volume is |

Notes:

- **RDS is the source of truth for structured data**, and it has **no backups
  configured**. It is not publicly accessible: only the EC2 instance can
  connect to it. Because the format tables are derived from the JSON exports,
  they can be rebuilt with `python services/build_lookup_db.py`. The `api_keys`
  table is the exception: it can't be rebuilt, so losing the database means
  losing every issued key (clients would need to generate new ones).
- **S3 is a manual, write-only archive of the reproducible files** (the source
  JSON, the vector DB, and `raw_xml/`). The application code never reads from
  or writes to S3. The files are stored there so they can be retrieved onto the
  EC2 instance if they're ever needed, rather than re-fetching and
  re-converting the upstream PRONOM / Library of Congress sources.
- The vector DB is also re-seeded at image build time
  (`python services/vector_db_setup.py` in the `dockerfile`), so the S3 copy is
  a recovery path, not something the running service depends on.
- **Redis is intentionally disposable.** It only holds cached scores and
  rate-limit counters, all of which are regenerated on demand.
- **Prometheus data is intentionally disposable.** Metrics are operational
  telemetry, not records; losing them only loses history.

## Prerequisites

- Python 3.11+
- [ExifTool](https://exiftool.org/) (system binary, not just the
  Python wrapper)
- PostgreSQL (local via `docker-compose.dev.yml`, or a hosted instance
  such as AWS RDS)
- Redis (local or containerized — see `docker-compose.yml`)
- An Anthropic API key
- PRONOM / Library of Congress export JSON files under `data/`:
  `pronom_formats.json`, `pronom_lifecycle.json`, `loc_formats.json`
  (read by `services/build_lookup_db.py`) and `loc_vector_docs.json`
  (read by `services/vector_db_setup.py`)

### macOS

```bash
brew install exiftool
```

### Debian/Ubuntu (used in the Docker image)

```bash
apt-get install libimage-exiftool-perl
```

## Local development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your-key-here
REDIS_URL=redis://:your-redis-password@localhost:6379
REDIS_PASSWORD=your-redis-password
POSTGRES_URL=postgresql://user:password@localhost:5432/aeterna
POSTGRES_DB=aeterna
POSTGRES_USER=user
POSTGRES_PASSWORD=password
```

`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` are only consumed by
the local Postgres container in `docker-compose.dev.yml`; the app itself only
needs `POSTGRES_URL`.

Start local Postgres and Redis:

```bash
docker compose -f docker-compose.dev.yml up -d postgres redis
```

Build the PostgreSQL format lookup tables and seed the vector store
(one-time, re-run whenever the source PRONOM/LoC JSON exports change):

```bash
python services/build_lookup_db.py
python services/vector_db_setup.py
```

> **Note:** `build_lookup_db.py` runs `DROP TABLE IF EXISTS` on the
> `extensions`, `format_relationships`, `formats`, and `loc_formats` tables
> before recreating them. It does **not** touch `api_keys`, but double-check
> your `POSTGRES_URL` before running it — especially against RDS.

Run the API:

```bash
uvicorn app:app --host 0.0.0.0 --port 3000 --reload
```

The service listens on `http://localhost:3000`. FastAPI also serves
interactive API docs at `http://localhost:3000/docs` (Swagger UI) and
`http://localhost:3000/redoc`.

Locally the API runs as a single process, so `PROMETHEUS_MULTIPROC_DIR` is
unset and metrics use the normal in-memory registry. You can inspect them
directly:

```bash
curl http://localhost:3000/metrics
```

The local dev compose file does not include Prometheus or Grafana.

Run the tests:

```bash
pip install pytest httpx
python -m pytest -q tests/test_endpoints.py
```

The tests are pre-deploy smoke tests: external dependencies (Postgres, Redis,
the Anthropic SDK, the metrics module, and the other service modules) are
mocked, so they run without any infrastructure.

## Authentication & rate limits

`/score` requires an API key, sent in the `X-API-Key` header. Keys look like
`atna_<64 hex chars>`. Only the SHA-256 hash of each key is stored in
PostgreSQL, so a key can't be recovered — **it is shown exactly once**, when
generated.

Get a key:

```bash
curl -X POST http://localhost:3000/generate-key
```

```json
{
  "api_key": "atna_...",
  "message": "Store this API key securely. It will not be shown again."
}
```

| Limit | Applies to | Default | Env vars |
|-------|-----------|---------|----------|
| `/score` | Per API key | 60 requests / 60 s | `SCORE_RATE_LIMIT`, `SCORE_RATE_WINDOW` |
| `/generate-key` | Per client IP | 3 keys / 3600 s | `KEYGEN_RATE_LIMIT`, `KEYGEN_RATE_WINDOW` |

Rate limiting is a fixed-window counter in Redis. The client IP is read from
`X-Real-IP` (set by Nginx), falling back to the socket peer address
(`request.client.host`) — a client-supplied `X-Forwarded-For` is deliberately
not trusted. `X-Real-IP` is only trustworthy when the app is reachable
exclusively through Nginx; the API container's port should not be exposed
publicly. Responses include an `X-RateLimit-Remaining` header, and `429`s from
`/score` also include `Retry-After`. Every rejection is counted in the
`aeterna_rate_limit_rejections_total` metric.

## Endpoints

| Method | Path            | Auth | Description |
|--------|-----------------|------|-------------|
| GET    | `/`             | —    | Basic service identity check |
| GET    | `/health`       | —    | Readiness probe. Checks required env vars, runs `SELECT 1` against PostgreSQL, and `PING`s Redis. Returns `200` when everything is healthy, `503` (`"status": "degraded"`) otherwise. |
| GET    | `/metrics`      | — (internal only) | Prometheus scrape endpoint (text exposition format). Not for public use: Nginx is expected to block it, and Prometheus reaches it over the internal Docker network. Hidden from `/docs`. See [Observability](#observability--metrics). |
| POST   | `/generate-key` | —    | Generate a new API key. Rate-limited per IP. Returns `201`, or `429` when the limit is hit. |
| POST   | `/score`        | `X-API-Key` | Upload a file (`multipart/form-data`, field `file`) and get its survivability score. Results are cached in Redis by file hash (7-day TTL) — a repeat upload of the same file skips the pipeline entirely. |

`/score` status codes: `200` success, `401` missing or invalid API key, `422`
missing `file` field (FastAPI request validation — this is also what a
client sends when no filename is attached to the file part, since most HTTP
clients treat an empty filename as an absent field), `429` rate limited,
`500` processing failure. The route also contains a `400` check for an
explicitly empty `file.filename`, but it's effectively unreachable through
standard multipart clients — see [Known limitations](#known-limitations).

Example:

```bash
curl -X POST http://localhost:3000/score \
  -H "X-API-Key: atna_your_key_here" \
  -F "file=@/path/to/some/file.png"
```

Response:

```json
{
  "json": {
    "sub_scores": {
      "openness": { "score": 0.9, "evidence": "..." },
      "adoption": { "score": 0.95, "evidence": "..." },
      "non_supersession": { "score": 0.95, "evidence": "..." },
      "metadata_completeness": { "score": 0.3, "evidence": "..." },
      "age_lifecycle_health": { "score": null, "evidence": "..." }
    },
    "missing_inputs": ["age_lifecycle_health"],
    "explanation": "...",
    "survivability_score": 85
  },
  "summary": "..."
}
```

## Docker (production)

The `dockerfile` is a multi-stage build. The builder stage installs the
CPU-only `torch` wheel first (to avoid pulling ~3GB of unused CUDA packages),
then the rest of `requirements.txt`, into a venv. The final stage installs the
`exiftool` system binary (required by `services/file_extraction.py`), copies
the app in, **seeds the Chroma vector store at build time**
(`python services/vector_db_setup.py`), and serves the app with `uvicorn`
(2 worker processes, 120 s keep-alive timeout) as a non-root user.

```bash
# Build and run
docker compose up --build -d

# Logs
docker compose logs -f

# Stop
docker compose down
```

Before running, make sure you have a `.env` file in the project root with
`ANTHROPIC_API_KEY`, `REDIS_URL`, `REDIS_PASSWORD`, `POSTGRES_URL`, and
`GRAFANA_ADMIN_PASSWORD` set (referenced by `docker-compose.yml` via
`env_file` / variable substitution). In production, `POSTGRES_URL` points at
the **AWS RDS** instance.

`docker-compose.yml` brings up six services:

| Service | Role |
|---------|------|
| `aeterna-api` | The FastAPI/uvicorn app itself (pulled from Docker Hub). Sets `PROMETHEUS_MULTIPROC_DIR` and mounts a `tmpfs` at `/tmp/prometheus_multiproc` so the two workers can share metric counters (wiped on every container start) |
| `redis` | Score cache and rate-limit store, password-protected via `REDIS_PASSWORD`, persisted to a named volume (`redis-data`) |
| `prometheus` | Scrapes `aeterna-api:3000/metrics` every 15 s; config mounted from `prometheus/prometheus.yml`; 15-day retention; data in the `prometheus-data` volume. Published only on `127.0.0.1:9090` |
| `grafana` | Dashboards. Admin user `admin`, password from `GRAFANA_ADMIN_PASSWORD`; Prometheus datasource provisioned from `grafana/provisioning/datasources/`; data in the `grafana-data` volume. Published only on `127.0.0.1:3001` |
| `nginx` | Reverse proxy — terminates HTTPS on 80/443 and forwards to `aeterna-api` on its internal port |
| `certbot` | Issues and renews the Let's Encrypt TLS certificate Nginx uses |

There is no Postgres container in the production compose file — the API
connects to RDS over the network. For local development,
`docker-compose.dev.yml` runs the API alongside Redis and a `postgres:17-alpine`
container (data persisted in the `postgres_data` volume).

The PostgreSQL lookup tables are populated once by running the build script
against the target database (RDS in production):

```bash
python services/build_lookup_db.py   # with POSTGRES_URL pointing at RDS
```

The Chroma vector store is already baked into the image at build time, so it
doesn't need a separate seeding step at deploy.

## Deployment (production)

Aeterna is deployed on a single **AWS EC2** instance with an **Elastic IP**
(so the public address survives instance stops/restarts), fronted by
**Nginx** for TLS termination and reverse-proxying, with certificates
issued and renewed by **Certbot**. Structured data lives in **AWS RDS
(PostgreSQL)**, which is only reachable from the EC2 instance, and copies of
the source JSON, vector DB, and raw XML are kept in **AWS S3** (see
[Data storage & backups](#data-storage--backups)). Prometheus and Grafana run
on the same instance (see [Observability](#observability--metrics)). Public DNS
is handled by **DuckDNS**, resolving to:

**https://aeterna-api.duckdns.org**

### CI/CD

Two GitHub Actions workflows gate every deploy:

**1. `tests` (`.github/workflows/tests.yml`)** — runs on every push to `main`
and on every pull request. It sets up Python 3.11, installs a minimal set of
dependencies (`fastapi`, `httpx`, `pytest`, `python-dotenv`,
`python-multipart`), and runs `python -m pytest -q tests/test_endpoints.py`.

**2. `Build and Deploy` (`.github/workflows/deploy.yml`)** — triggered by
`workflow_run` when the `tests` workflow **completes**, and only proceeds if
the tests **succeeded** on a push. It then:

1. Builds the Docker image and pushes it to **Docker Hub**
   (`abiolar/aeterna-rag-api:latest`).
2. Copies the deployment config to the EC2 instance over SCP:
   `docker-compose.yml`, `nginx/default.conf`, `prometheus/prometheus.yml`,
   and `grafana/provisioning/datasources/prometheus.yml`.
3. SSHes into the EC2 instance, prunes stale containers/images/build cache
   (and logs disk usage before and after, since the instance has limited
   disk), then runs `docker compose pull` followed by `docker compose up -d`,
   and restarts the `nginx` container to pick up any config changes.

This means deploys are just `git push` to `main` — a failing test run blocks
the deploy, and there are no manual SSH or Docker commands for a routine
update. The EC2 host only needs the four config files above and its `.env`
(including `GRAFANA_ADMIN_PASSWORD`); the application image itself is always
pulled fresh from Docker Hub.

**Required GitHub Actions secrets:** `DOCKERHUB_USERNAME`,
`DOCKERHUB_TOKEN`, `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`.

## Monitoring

Production has two complementary layers of monitoring:

- **External uptime** (UptimeRobot) — "is it reachable right now?"
- **Internal metrics** (Prometheus + Grafana) — "how is it behaving over
  time?" See [Observability & metrics](#observability--metrics).

### Uptime (UptimeRobot)

Production uptime is monitored with **UptimeRobot**, which polls the public
`/health` endpoint (`https://aeterna-api.duckdns.org/health`).

`/health` does more than confirm the process is up: it runs `SELECT 1` against
PostgreSQL (RDS) and `PING`s Redis, and returns `503` if either fails or a
required env var is missing. So the monitor flags a dead database or cache as
well as a dead API.

Because Docker's `restart: unless-stopped` and the container `HEALTHCHECK` only
handle local restarts, UptimeRobot is the external check that the whole path
(DNS, Nginx, TLS, API, RDS, Redis) is reachable from the outside.

## Observability & metrics

The API exposes Prometheus metrics at `GET /metrics`. All metric definitions
and `record_*` helper functions live in `services/metrics.py`; `app.py` and
`services/llm_inference.py` call the helpers when something happens.

### What's measured

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `aeterna_http_requests_total` | Counter | `method`, `path`, `status` | Every HTTP request handled |
| `aeterna_http_request_duration_seconds` | Histogram | `method`, `path` | Request latency (buckets up to 120 s, since `/score` calls an LLM) |
| `aeterna_cache_events_total` | Counter | `result` (`hit`/`miss`) | Redis score-cache lookups on `/score` |
| `aeterna_rate_limit_rejections_total` | Counter | `endpoint` (`score`/`generate-key`) | Requests rejected with `429` |
| `aeterna_inference_duration_seconds` | Histogram | — | Time in `run_inference()` (ExifTool + DB lookup + vector search + Anthropic call) |
| `aeterna_scoring_failures_total` | Counter | `reason` (`invalid_llm_json`/`internal`) | Failed `/score` attempts |
| `aeterna_llm_tokens_total` | Counter | `direction` (`input`/`output`) | Anthropic API tokens used — the cost signal |

Design notes:

- **Label cardinality is kept small and fixed.** The HTTP middleware labels
  requests with the route *template* (e.g. `/score`), not the raw URL, and
  groups unknown URLs (bots probing `/admin`, etc.) as `unmatched`. File
  names and API keys are never used as labels.
- **A request that crashes is recorded as a `500`.**
- **`/metrics` scrapes are not counted**, so Prometheus doesn't add noise to
  its own data.
- **Known label values are pre-created at 0**, so Prometheus doesn't miss the
  first increase of a series when computing rates.
- **Multiprocess mode.** In Docker, uvicorn runs 2 workers, each with its own
  memory. When `PROMETHEUS_MULTIPROC_DIR` is set (as in `docker-compose.yml`),
  each worker writes its counters into files in that shared folder and
  `/metrics` merges them, so the numbers cover both workers. The folder is a
  `tmpfs`, so it starts empty on every container start. When the variable is
  unset (local single-process dev), the normal in-memory registry is used.

### Accessing Prometheus and Grafana

Both are bound to `127.0.0.1` on the EC2 host and are **not** exposed
publicly. To reach them from your machine, use an SSH tunnel:

```bash
ssh -L 3001:localhost:3001 -L 9090:localhost:9090 <EC2_USER>@<EC2_HOST>
```

Then open `http://localhost:3001` (Grafana; log in as `admin` with
`GRAFANA_ADMIN_PASSWORD`) and `http://localhost:9090` (Prometheus).

The Prometheus datasource is provisioned automatically, but dashboards are
not — they are built in the Grafana UI and stored in the `grafana-data`
volume.

### Keeping `/metrics` private

`/metrics` has no authentication of its own. It is meant to be reachable only
over the internal Docker network (Prometheus → `aeterna-api:3000`), and Nginx
is expected to block it on the public hostname. After any Nginx config change,
verify:

```bash
curl -i https://aeterna-api.duckdns.org/metrics   # should NOT return metrics
```

## Error handling

`/score` never returns internal detail (stack traces, LLM raw
output, file paths, library errors) in the HTTP response — everything
specific is logged server-side via the `aeterna` logger, and the
client only receives a generic message with an appropriate status
code (400 for bad requests, 401 for auth failures, 429 for rate limits,
500 for processing failures). Uploaded temp files are removed in a `finally`
block whether or not scoring succeeds.

Note that request-validation failures (e.g. a missing `file` field) are
handled by FastAPI itself and return its standard `422` response body.

## Known limitations

- **`age_lifecycle_health` is frequently null.** PRONOM populates
  `release_date` for only a small fraction of formats and
  `withdrawn_date` for almost none, so this term is often excluded
  from the score (see the weight-renormalization logic in
  `compute_survivability_score` in `services/llm_inference.py`).
- **`related_puid` in `format_relationships` is not yet joinable.**
  It currently stores PRONOM's internal numeric `FormatID` rather
  than a `fmt/___`-style PUID, so it can't be joined back against
  `formats.puid` yet.
- **No fallback yet for formats absent from the LoC vector store.**
  If a PRONOM format has no corresponding Library of Congress entry,
  the vector search step currently just returns whatever it finds as
  the nearest (possibly irrelevant) semantic neighbors, rather than
  a distance-threshold check or a PRONOM-only rule-based score.
- **The file extension used for the format lookup comes from the
  client-supplied filename**, not from inspecting file contents. The saved
  temp filename itself is randomly generated (only the extension is taken
  from the client), so concurrent uploads no longer collide in
  `temp_uploads/` — but a mislabeled or absent extension will still cause an
  incorrect or failed format lookup. Content-based format detection isn't
  used.
- **The test suite is smoke-level only.** Every service module (including
  `services/metrics`) is mocked, so the tests cover routing, auth, and
  rate-limit branching in `app.py` but not the scoring pipeline, the Postgres
  queries, the LLM call, or the real metric definitions.
- **The `400` empty-filename check in `/score` is effectively dead code.**
  `file: UploadFile = File(...)` makes the field required, so a request
  with no `file` part gets `422` from FastAPI before the handler runs at
  all — the auth check, rate-limit check, and the `file.filename == ""`
  check are all skipped on that path. Standard multipart clients (browsers,
  `curl -F`, `httpx`, `requests`) also treat a file part with an empty
  filename as an absent field, producing the same `422` rather than reaching
  the `400` branch. Reaching the `400` path requires a client that sends a
  filename explicitly present but empty, which isn't something normal
  tooling does.
- **`/metrics` is unauthenticated at the application level.** Its privacy
  relies on Nginx blocking it and on the API container's port not being
  exposed publicly.
- **Prometheus and Grafana images are unpinned (`:latest`).** Pin specific
  versions once the setup is stable so a redeploy can't silently change them.
- **No alert rules yet.** Prometheus's `evaluation_interval` is set but no
  alerting rules or Alertmanager are configured; the dashboards are for
  inspection, and UptimeRobot remains the only alerting path.

## Project structure

```
.
├── app.py                    # FastAPI entrypoint (/, /health, /metrics, /generate-key, /score) + metrics middleware
├── services/
│   ├── __init__.py
│   ├── auth.py                # API key generation + validation (PostgreSQL, SHA-256 hashed)
│   ├── cache.py               # Redis score cache (SHA-256 file hash, 7-day TTL) + rate limiter
│   ├── file_extraction.py    # ExifTool metadata + extension helpers
│   ├── build_lookup_db.py    # PRONOM/LoC PostgreSQL lookup DB build + query
│   ├── vector_db_setup.py    # Chroma vector store setup + query
│   ├── data_pipeline.py      # Aggregates context for one file
│   ├── system_prompt.py      # Builds the LLM sub-scoring prompt
│   ├── llm_inference.py      # Calls the Anthropic API + computes the final score
│   └── metrics.py            # Prometheus metric definitions + record_* helpers + /metrics rendering
├── tests/
│   └── test_endpoints.py      # Pre-deploy smoke tests (all services mocked)
├── nginx/
│   └── default.conf          # Nginx reverse-proxy + TLS config
├── prometheus/
│   └── prometheus.yml        # Scrape config (API + Prometheus itself, 15 s interval)
├── grafana/
│   └── provisioning/
│       └── datasources/
│           └── prometheus.yml # Auto-provisions the Prometheus datasource in Grafana
├── data/                     # PRONOM/LoC export JSON (build_lookup_db.py, vector_db_setup.py inputs); copy stored in S3
├── db/                       # Generated on build — not committed
│   └── aeterna_vector_db/    # Persistent Chroma vector store (vector_db_setup.py output); copy stored in S3
├── raw_xml/                  # Raw PRONOM/LoC source XML, pre-JSON-conversion; copy stored in S3
├── temp_uploads/             # Scratch space for in-flight /score uploads, cleaned up per-request
├── test_files/               # Sample files for manually exercising /score
├── .github/
│   └── workflows/
│       ├── tests.yml          # CI: pytest smoke tests on push/PR
│       └── deploy.yml         # CD: after tests pass, build + push image, deploy to EC2
├── dockerfile
├── docker-compose.yml         # Production stack (API, Redis, Prometheus, Grafana, Nginx, Certbot)
├── docker-compose.dev.yml     # Local dev stack (API, Redis, Postgres)
├── pytest.ini
├── .dockerignore
├── .gitignore
├── requirements.txt
└── README.md
```