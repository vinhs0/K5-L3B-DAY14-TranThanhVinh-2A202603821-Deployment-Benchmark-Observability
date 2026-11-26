"""
Rate limiting (tiêu chí 7).

  • Mỗi API key tối đa RATE_LIMIT_PER_MINUTE request trong 60 giây (sliding window)
  • Vượt giới hạn → HTTP 429 kèm header Retry-After
  • State lưu trong Redis (sorted set) để mọi worker / instance dùng chung và vẫn
    còn sau khi restart agent
"""
import hashlib
import time
import uuid

import redis
from fastapi import HTTPException

from app.config import settings
from app.redis_client import r

WINDOW_SECONDS = 60


def _bucket(key: str) -> str:
    # Không lưu API key thô trong Redis — chỉ lưu hash
    return "ratelimit:" + hashlib.sha256(key.encode()).hexdigest()[:24]


def check_rate_limit(key: str) -> None:
    bucket = _bucket(key)
    now = time.time()
    try:
        pipe = r.pipeline()
        pipe.zremrangebyscore(bucket, 0, now - WINDOW_SECONDS)   # bỏ request ngoài cửa sổ
        pipe.zcard(bucket)                                         # đếm request trong cửa sổ
        _, count = pipe.execute()
        if count >= settings.rate_limit_per_minute:
            oldest = r.zrange(bucket, 0, 0, withscores=True)
            retry = WINDOW_SECONDS
            if oldest:
                retry = max(1, int(oldest[0][1] + WINDOW_SECONDS - now) + 1)
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {settings.rate_limit_per_minute} requests/minute",
                headers={"Retry-After": str(retry)},
            )
        pipe = r.pipeline()
        pipe.zadd(bucket, {f"{now}:{uuid.uuid4().hex[:8]}": now})
        pipe.expire(bucket, WINDOW_SECONDS + 1)
        pipe.execute()
    except redis.RedisError:
        # Không có Redis thì không đếm được → từ chối thay vì cho qua không giới hạn
        raise HTTPException(status_code=503, detail="Rate limiter unavailable")
