"""
Prometheus metrics for the Aeterna API.
"""

import os

_MULTIPROC_DIR = os.getenv("PROMETHEUS_MULTIPROC_DIR")
if _MULTIPROC_DIR:
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

HTTP_REQUESTS_TOTAL = Counter(
    "aeterna_http_requests_total",
    "Total HTTP requests handled by the API.",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "aeterna_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

CACHE_EVENTS_TOTAL = Counter(
    "aeterna_cache_events_total",
    "Score cache lookups, split by result (hit/miss).",
    ["result"],
)

RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "aeterna_rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
    ["endpoint"],
)

INFERENCE_DURATION = Histogram(
    "aeterna_inference_duration_seconds",
    "Time spent running the full scoring pipeline for one file.",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

SCORING_FAILURES_TOTAL = Counter(
    "aeterna_scoring_failures_total",
    "Failed /score attempts, split by reason.",
    ["reason"],
)

LLM_TOKENS_TOTAL = Counter(
    "aeterna_llm_tokens_total",
    "Anthropic API tokens used, split by direction (input/output).",
    ["direction"],
)

for _result in ("hit", "miss"):
    CACHE_EVENTS_TOTAL.labels(result=_result)
for _endpoint in ("score", "generate-key"):
    RATE_LIMIT_REJECTIONS_TOTAL.labels(endpoint=_endpoint)
for _reason in ("invalid_llm_json", "internal"):
    SCORING_FAILURES_TOTAL.labels(reason=_reason)
for _direction in ("input", "output"):
    LLM_TOKENS_TOTAL.labels(direction=_direction)


# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------

def record_http_request(method: str, path: str, status_code: int, duration: float):
    """Count one finished HTTP request and record how long it took."""
    HTTP_REQUESTS_TOTAL.labels(
        method=method, path=path, status=str(status_code)
    ).inc()
    HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(duration)


def record_cache_result(hit: bool):
    """Record whether a /score request was served from the Redis cache."""
    CACHE_EVENTS_TOTAL.labels(result="hit" if hit else "miss").inc()


def record_rate_limit_rejection(endpoint: str):
    """Record one request rejected with a 429."""
    RATE_LIMIT_REJECTIONS_TOTAL.labels(endpoint=endpoint).inc()


def record_inference_duration(seconds: float):
    """Record how long run_inference() took."""
    INFERENCE_DURATION.observe(seconds)


def record_scoring_failure(reason: str):
    """Record a failed /score attempt ("invalid_llm_json" or "internal")."""
    SCORING_FAILURES_TOTAL.labels(reason=reason).inc()


def record_llm_usage(input_tokens: int, output_tokens: int):
    """Record the tokens used by one Anthropic API call."""
    LLM_TOKENS_TOTAL.labels(direction="input").inc(input_tokens)
    LLM_TOKENS_TOTAL.labels(direction="output").inc(output_tokens)


# --------------------------------------------------------------------------
# Rendering 
# --------------------------------------------------------------------------

def get_metrics():
    """
    Render all metrics in Prometheus's text format.

    Returns:
        (payload_bytes, content_type) - app.py sends these straight back
        as the HTTP response.
    """
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY

    return generate_latest(registry), CONTENT_TYPE_LATEST