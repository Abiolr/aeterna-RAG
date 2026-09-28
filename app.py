"""
Aeterna RAG API — FastAPI entrypoint.

Exposes four HTTP endpoints:

    GET  /            - basic service identity/info
    GET  /health      - liveness/readiness probe
    POST /generate-key - generate a new API key (IP-rate-limited)
    POST /score       - authenticated file scoring endpoint, rate-limited
                       per API key.

API authentication:

    Clients must provide:
        X-API-Key: atna_<key>

API keys are generated with cryptographically secure randomness and only
their SHA-256 hashes are stored in PostgreSQL.
"""

import os
import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, status, Request, Response, UploadFile, File

from services.auth import (
    generate_api_key,
    is_valid_api_key,
    _hash_api_key,
    check_postgres_connection,
)

from services.llm_inference import run_inference

from services.cache import (
    get_cached_score,
    cache_score,
    check_rate_limit,
    check_redis_connection,
)

app = FastAPI()

app.state.upload_folder = "temp_uploads"

os.makedirs(app.state.upload_folder, exist_ok=True)

logger = logging.getLogger("aeterna")

_START_TIME = time.time()

REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "REDIS_URL",
    "POSTGRES_URL",
]

# --------------------------------------------------------------------------
# Rate-limit configuration
# --------------------------------------------------------------------------

# /score: maximum requests allowed for one API key in one minute.
SCORE_RATE_LIMIT = int(os.getenv("SCORE_RATE_LIMIT", "60"))
SCORE_RATE_WINDOW = int(os.getenv("SCORE_RATE_WINDOW", "60"))

# /generate-key: maximum keys that one IP can generate in one hour.
KEYGEN_RATE_LIMIT = int(os.getenv("KEYGEN_RATE_LIMIT", "3"))
KEYGEN_RATE_WINDOW = int(os.getenv("KEYGEN_RATE_WINDOW", "3600"))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _get_uptime_seconds() -> float:
    return round(time.time() - _START_TIME, 2)

def _get_env_status() -> dict:
    return {
        name: bool(os.getenv(name))
        for name in REQUIRED_ENV_VARS
    }


def _get_client_ip(request: Request):
    """
    Get the client IP.
    """
    return (
        request.headers.get("X-Real-IP")
        or (request.client.host if request.client else None)
        or "unknown"
    )

# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def root():
    return {
            "service": "aeterna-api",
            "message": "Aeterna API is running.",
            "status": "ok",
        }

@app.get("/health")
def healthcheck(response: Response):
    """
    Check API readiness and required infrastructure dependencies.

    PostgreSQL:
        Executes SELECT 1 against the configured PostgreSQL database.

    Redis:
        Executes PING against the configured Redis instance.

    Environment variables:
        Checks that all required environment variables are present.

    Returns:
        200 if all required dependencies are healthy.
        503 if any required dependency is unavailable.
    """

    env_status = _get_env_status()

    # ----------------------------------------------------------------------
    # PostgreSQL healthcheck
    # ----------------------------------------------------------------------

    postgres_healthy = False

    if env_status["POSTGRES_URL"]:
        try:
            postgres_healthy = check_postgres_connection()
        except Exception:
            logger.exception("PostgreSQL healthcheck failed.")

    # ----------------------------------------------------------------------
    # Redis healthcheck
    # ----------------------------------------------------------------------

    redis_healthy = False

    if env_status["REDIS_URL"]:
        try:
            redis_healthy = check_redis_connection()
        except Exception:
            logger.exception("Redis healthcheck failed.")

    # ----------------------------------------------------------------------
    # Determine overall health
    # ----------------------------------------------------------------------

    dependencies = {
        "postgres": "healthy" if postgres_healthy else "unhealthy",
        "redis": "healthy" if redis_healthy else "unhealthy",
    }

    all_healthy = (
        all(env_status.values())
        and postgres_healthy
        and redis_healthy
    )

    health = {
        "status": "healthy" if all_healthy else "degraded",
        "uptime": _get_uptime_seconds(),
        "environment": env_status,
        "dependencies": dependencies,
    }

    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return health

@app.post("/generate-key", status_code=status.HTTP_201_CREATED)
def create_api_key(request: Request, response: Response):
    """
    Generate a new API key.

    No existing API key is required. To reduce abuse, key generation is
    rate-limited by client IP using Redis.
    """

    client_ip = _get_client_ip(request)

    try:
        allowed, remaining = check_rate_limit(
            identifier=client_ip,
            limit=KEYGEN_RATE_LIMIT,
            window_seconds=KEYGEN_RATE_WINDOW,
            prefix="keygen",
        )

        if not allowed:
            response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
            return {
                "error": (
                    "Too many API keys generated. "
                    "Please try again later."
                )
            }

        api_key = generate_api_key()

        generated_key = {
            "api_key": api_key,
            "message": (
                "Store this API key securely. "
                "It will not be shown again."
            ),
        }

        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return generated_key

    except Exception:
        logger.exception("Failed to generate API key.")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

        return {
            "error": "Unable to generate API key."
        }


@app.post("/score")
def score_file(request: Request, response: Response, file: UploadFile = File(...)):
    """
    Accept a single uploaded file and return its survivability score.

    Authentication:
        X-API-Key: atna_<key>

    Rate limit:
        SCORE_RATE_LIMIT requests per SCORE_RATE_WINDOW seconds per API key.
    """

    api_key = request.headers.get("X-API-Key")

    if not api_key:
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"error": "API key required."}

    # ----------------------------------------------------------------------
    # API key authentication
    # ----------------------------------------------------------------------

    try:
        if not is_valid_api_key(api_key):
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return {"error": "Invalid API key."}

    except Exception:
        logger.exception("API key validation failed.")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"error": "Unable to authenticate request."}

    # ----------------------------------------------------------------------
    # Rate limiting
    # ----------------------------------------------------------------------

    try:
        key_hash = _hash_api_key(api_key)

        allowed, remaining = check_rate_limit(
            identifier=key_hash,
            limit=SCORE_RATE_LIMIT,
            window_seconds=SCORE_RATE_WINDOW,
            prefix="score",
        )

        if not allowed:
            response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
            response.headers["Retry-After"] = str(SCORE_RATE_WINDOW)
            response.headers["X-RateLimit-Remaining"] = "0"
            return {"error": "Rate limit exceeded. Please try again later."}

    except Exception:
        logger.exception("Rate-limit check failed.")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"error": "Unable to process request."}

    # ----------------------------------------------------------------------
    # File validation
    # ----------------------------------------------------------------------

    if file.filename == "":
        response.status_code = status.HTTP_400_BAD_REQUEST
        return {"error": "No file selected"}

    filename = f"{uuid.uuid4().hex}{Path(file.filename).suffix}"

    file_path = os.path.join(
        app.state.upload_folder,
        filename,
    )

    with open(file_path, "wb") as f:
        f.write(file.file.read())

    result_text = None

    # ----------------------------------------------------------------------
    # Scoring
    # ----------------------------------------------------------------------

    try:
        cached_response = get_cached_score(file_path)

        if cached_response is not None:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            return cached_response

        result_text = run_inference(file_path)

        parts = result_text.split("---", 1)

        raw_json = (
            parts[0]
            .strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )

        score_data = json.loads(raw_json)

        summary = (
            parts[1].strip()
            if len(parts) > 1
            else ""
        )

        results = {
            "json": score_data,
            "summary": summary,
        }

        cache_score(file_path, results)

        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return results

    except json.JSONDecodeError:
        logger.exception(
            "Failed to parse LLM JSON output for file '%s'. "
            "Raw output: %r",
            filename,
            result_text,
        )

        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"error": "Failed to process file. Please try again."}

    except Exception:
        logger.exception(
            "Unexpected error while scoring file '%s'.",
            filename,
        )

        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {
            "error": (
                "An internal error occurred while processing "
                "the file."
            )
        }

    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)

            except OSError:
                logger.exception(
                    "Failed to remove temp upload '%s'.",
                    file_path,
                )