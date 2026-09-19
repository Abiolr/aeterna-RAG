"""
Aeterna RAG API — Flask entrypoint.

Exposes three HTTP endpoints:
    GET  /        - basic service identity/info
    GET  /health  - liveness/readiness probe (uptime + config presence)
    POST /score   - upload a file, run it through the survivability
                    scoring pipeline (services.llm_inference.run_inference),
                    and return the parsed JSON score + markdown summary.

Error-handling policy for this module:
    - Every exception is logged server-side (with a stack trace, via
      `logger.exception`) so operators can diagnose the real failure.
    - Nothing exception-specific (stack traces, exception messages, raw
      LLM output, file paths) is ever put in the HTTP response body.
      Clients only ever see a short, generic message plus an
      appropriate status code. This avoids leaking internal
      implementation details (library versions, file system layout,
      prompt content, API errors) to callers of the API.
"""

import os
import json
import logging
import time
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
from services.llm_inference import run_inference
from services.cache import get_cached_score, cache_score

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'temp_uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Standard library logger. In production this should be configured
# (handlers/formatters/log level) by whatever runs the app (gunicorn,
# a platform log driver, etc.) — we just log through the standard
# "aeterna" named logger here so that configuration is possible.
logger = logging.getLogger("aeterna")

# Record process start time so /health can report uptime.
_START_TIME = time.time()

# Environment variables the service depends on at runtime. Used by
# /health to report whether the deployment is fully configured, without
# ever exposing the actual values (secrets) in the response.
REQUIRED_ENV_VARS = ["ANTHROPIC_API_KEY", "REDIS_URL", "POSTGRES_URL"]


def _get_uptime_seconds() -> float:
    """Return how many seconds this process has been running."""
    return round(time.time() - _START_TIME, 2)


def _get_env_status() -> dict:
    """
    Check presence (not value) of each variable in REQUIRED_ENV_VARS.

    Returns a dict like:
        {"ANTHROPIC_API_KEY": True}
    where True means the variable is set (non-empty) in the current
    environment. Values themselves are never returned, only booleans,
    so this is safe to expose in a health endpoint.
    """
    return {name: bool(os.getenv(name)) for name in REQUIRED_ENV_VARS}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def root():
    """
    Basic service identity endpoint.

    Not a substitute for /health — this just confirms the API process
    is reachable and identifies itself. Kept intentionally lightweight
    (no I/O, no dependency checks) so it always responds even if a
    downstream dependency (DB, vector store, LLM API) is unhealthy.
    """
    return jsonify({
        "service": "aeterna-api",
        "message": "Aeterna API is running.",
        "status": "ok",
    }), 200


@app.route('/health', methods=['GET'])
def healthcheck():
    """
    Liveness/readiness probe.

    Reports:
        - status: "healthy" if all required env vars are present,
          "degraded" otherwise (process is up, but a dependency the
          scoring pipeline needs is missing/misconfigured).
        - uptime_seconds: how long this process has been running.
        - environment: presence (true/false only, never values) of each
          required environment variable, so misconfiguration is visible
          to monitoring without exposing secrets.

    Intended for use by load balancers, container orchestrators, or
    uptime monitors. Always returns 200 so orchestrators can read the
    body to decide readiness themselves; if you need a hard fail
    signal instead, gate on the "status" field.
    """
    env_status = _get_env_status()
    all_present = all(env_status.values())

    return jsonify({
        "status": "healthy" if all_present else "degraded",
        "uptime": _get_uptime_seconds(),
        "environment": env_status,
    }), 200


@app.route('/score', methods=['POST'])
def score_file():
    """
    Accept a single uploaded file and return its survivability score.

    Expects a multipart/form-data POST with the file under the form
    field name "file".

    On success (200):
        {
            "json": <parsed survivability scoring object from the LLM>,
            "summary": <markdown summary string>
        }

    On failure, returns a generic client-safe error message and an
    appropriate status code (400 for bad requests, 500 for anything
    that fails during processing). The specific cause is always logged
    server-side via `logger.exception`/`logger.warning` — it is never
    included in the HTTP response, since the underlying detail (parser
    errors, LLM API errors, file-system errors) could reveal internal
    implementation details to the caller.

    The uploaded file is always removed from disk before returning,
    whether processing succeeded or failed (see `finally` block).
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file key in request"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    # secure_filename strips path separators/unsafe characters so the
    # upload can't be used to write outside UPLOAD_FOLDER.
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    result_text = None
    try:
        cached_response = get_cached_score(file_path)
        if cached_response is not None:
            return jsonify(cached_response)

        result_text = run_inference(file_path)

        # The LLM is instructed to emit "<json>\n---\n<markdown summary>".
        parts = result_text.split('---', 1)

        raw_json = parts[0].strip().removeprefix('```json').removesuffix('```').strip()
        score_data = json.loads(raw_json)

        summary = parts[1].strip() if len(parts) > 1 else ""

        results = {
            "json": score_data,
            "summary": summary
        }

        cache_score(file_path, results)

        return jsonify({
            "json": score_data,
            "summary": summary
        }), 200

    except json.JSONDecodeError:
        # The LLM's output didn't parse as JSON in the expected shape.
        # Log the actual raw output server-side for debugging, but
        # never send it back to the client — it may contain prompt
        # internals or unpredictable model output.
        logger.exception(
            "Failed to parse LLM JSON output for file '%s'. Raw output: %r",
            filename, result_text,
        )
        return jsonify({
            "error": "Failed to process file. Please try again."
        }), 500

    except Exception:
        # Catch-all for anything else (LLM API errors, file-extraction
        # errors, vector DB errors, etc.). Full detail + stack trace
        # goes to the server log only.
        logger.exception(
            "Unexpected error while scoring file '%s'.", filename,
        )
        return jsonify({
            "error": "An internal error occurred while processing the file."
        }), 500

    finally:
        # Always clean up the temp upload, success or failure.
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                logger.exception(
                    "Failed to remove temp upload '%s'.", file_path,
                )


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app.run(debug=False, port=3000)