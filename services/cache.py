import os
import json
import hashlib
from dotenv import load_dotenv
from redis import Redis

load_dotenv(override=True)

REDIS_URL = os.getenv("REDIS_URL")
r = Redis.from_url(REDIS_URL, decode_responses=True)


def get_cached_score(file_path):
    with open(file_path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()

    cached = r.get(f"score:{file_hash}")
    return json.loads(cached) if cached else None


def cache_score(file_path, result_json):
    with open(file_path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()

    r.setex(
        f"score:{file_hash}",
        60 * 60 * 24 * 7,
        json.dumps(result_json),
    )


def check_rate_limit(identifier, limit, window_seconds, prefix):
    """
    Fixed-window Redis rate limiter.

    Returns:
        (allowed, remaining)
    """
    key = f"rate:{prefix}:{identifier}"

    count = r.incr(key)

    if count == 1:
        r.expire(key, window_seconds)

    remaining = max(0, limit - count)

    return count <= limit, remaining
