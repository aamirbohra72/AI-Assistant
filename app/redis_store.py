import json
from datetime import datetime
from typing import Any

import certifi
from redis.asyncio import Redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from app.config import get_settings

SCHEDULE_KEY = "interviews:scheduled"  # ZSET member=interview_id score=unix ts
SCORING_KEY = "interviews:scoring"  # LIST of interview_ids awaiting scoring
SCORING_RETRY_KEY = "interviews:scoring_retry"  # ZSET member=interview_id score=retry-at unix ts

_redis: Redis | None = None


def _state_key(interview_id: int) -> str:
    return f"interview:{interview_id}:state"


def _reconnect_key(interview_id: int) -> str:
    return f"interview:{interview_id}:reconnects"


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            get_settings().REDIS_URL,
            decode_responses=True,
            # certifi bundle: OS trust stores can lag behind newer CA chains.
            ssl_ca_certs=certifi.where(),
            socket_connect_timeout=5,
            socket_timeout=10,
            health_check_interval=30,
            retry=Retry(ExponentialBackoff(cap=2, base=0.1), 3),
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
        )
    return _redis


async def close() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def enqueue_call(interview_id: int, when: datetime, *, only_if_missing: bool = False) -> None:
    await get_redis().zadd(SCHEDULE_KEY, {str(interview_id): when.timestamp()}, nx=only_if_missing)


async def claim_due_calls(now_ts: float, limit: int = 20) -> list[int]:
    return await _claim_due(SCHEDULE_KEY, now_ts, limit)


async def claim_due_scoring_retries(now_ts: float, limit: int = 20) -> list[int]:
    return await _claim_due(SCORING_RETRY_KEY, now_ts, limit)


async def schedule_scoring_retry(interview_id: int, retry_at_ts: float) -> None:
    redis = get_redis()
    key = f"interview:{interview_id}:scoring_attempts"
    await redis.incr(key)
    await redis.expire(key, 86400)
    await redis.zadd(SCORING_RETRY_KEY, {str(interview_id): retry_at_ts})


async def scoring_attempts(interview_id: int) -> int:
    value = await get_redis().get(f"interview:{interview_id}:scoring_attempts")
    return int(value) if value else 0


async def _claim_due(key: str, now_ts: float, limit: int) -> list[int]:
    redis = get_redis()
    due = await redis.zrangebyscore(key, "-inf", now_ts, start=0, num=limit)
    claimed = []
    for member in due:
        # ZREM is atomic, so only one worker ever wins a given interview.
        if await redis.zrem(key, member):
            claimed.append(int(member))
    return claimed


async def enqueue_scoring(interview_id: int) -> None:
    await get_redis().lpush(SCORING_KEY, str(interview_id))


async def pop_scoring() -> int | None:
    value = await get_redis().rpop(SCORING_KEY)
    return int(value) if value else None


async def save_state(interview_id: int, state: dict[str, Any]) -> None:
    await get_redis().set(_state_key(interview_id), json.dumps(state), ex=get_settings().CALL_STATE_TTL_SECONDS)


async def load_state(interview_id: int) -> dict[str, Any] | None:
    raw = await get_redis().get(_state_key(interview_id))
    return json.loads(raw) if raw else None


async def delete_state(interview_id: int) -> None:
    await get_redis().delete(_state_key(interview_id), _reconnect_key(interview_id))


async def incr_reconnects(interview_id: int) -> int:
    redis = get_redis()
    count = await redis.incr(_reconnect_key(interview_id))
    await redis.expire(_reconnect_key(interview_id), get_settings().CALL_STATE_TTL_SECONDS)
    return int(count)
