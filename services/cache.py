import os
import json
import hashlib
from dotenv import load_dotenv
from redis import Redis

load_dotenv()
REDIS_URL = os.getenv("REDIS_URL")
r = Redis.from_url(REDIS_URL, decode_responses=True)

def get_cached_score(file_path):
    file_hash = hashlib.sha256(open(file_path, 'rb').read()).hexdigest()
    cached = r.get(f"score:{file_hash}")
    return json.loads(cached) if cached else None

def cache_score(file_path, result_json):
    file_hash = hashlib.sha256(open(file_path, 'rb').read()).hexdigest()
    r.setex(f"score:{file_hash}", 60 * 60 * 24 * 7, json.dumps(result_json))  # 1 week TTL
