"""Redis client used by the gate, calculator, and monitor.

The governor keeps every piece of hot-path state in Redis. Per ADR-007 (which
supersedes ADR-002), the default store is the local Redis on
``127.0.0.1:6379`` — loopback, sub-millisecond, zero new infra. The original
plan (ADR-002) assumed Upstash REST credentials from example-extractor could be
reused; that instance was already dead when this project started, so
Upstash-over-REST is now only a fallback URL-construction path rather than
the primary transport.

The class is still named ``UpstashClient`` because its public surface
(``get``, ``set``, ``incrbyfloat``, ``sadd``, ``lpush``, ``ltrim``, ...) maps
1:1 onto native Redis commands — every consumer (``server.app``,
``server.gate``, ``server.monitor``, ``server.calculator.calibration``)
imports it by that name, and ``tests/test_app.py`` ships a ``FakeUpstash``
that mirrors the same surface. Renaming to ``RedisClient`` is a follow-up
cleanup after the rollout is stable (see ADR-007 §Rollout).

Transport
---------
Wraps :class:`redis.asyncio.Redis`. Connection URLs are resolved via
:func:`redis.asyncio.Redis.from_url`:

* ``redis://127.0.0.1:6379/0``       — default (local Redis, ADR-007)
* ``rediss://default:TOKEN@HOST:6379`` — TLS endpoint; also what we synthesize
  from the legacy ``UPSTASH_REDIS_REST_URL`` + ``UPSTASH_REDIS_REST_TOKEN``
  pair when ``REDIS_URL`` is unset, preserving the original ADR-002 escape
  hatch if someone ever re-provisions an Upstash DB.

Failure posture
---------------
Every connection / timeout / response error from redis-py is caught and
re-raised as :class:`UpstashUnavailable`. The gate catches that exception and
returns a denial with code ``STATE_UNAVAILABLE`` — fail-CLOSED at the gate
level because we cannot trust any quota reading we could not make. The
wrapper library above the gate is still fail-open (ADR-005), which is the
overall system posture.
"""

from __future__ import annotations

import os
from typing import Any, Final
from typing import Set as _Set  # builtins.set shadowed inside the client class
from urllib.parse import urlparse

from redis import asyncio as redis_asyncio
from redis.exceptions import (
    ConnectionError as _RedisConnectionError,
)
from redis.exceptions import (
    RedisError,
    TimeoutError as _RedisTimeoutError,
)


# ─── Constants ───────────────────────────────────────────────────────────────
UPSTASH_REST_TIMEOUT_SECONDS: Final[float] = 3.0
UPSTASH_MAX_RETRIES: Final[int] = 2
REDIS_URL_ENV: Final[str] = "REDIS_URL"
REDIS_URL_DEFAULT: Final[str] = "redis://127.0.0.1:6379/0"
UPSTASH_REST_URL_ENV: Final[str] = "UPSTASH_REDIS_REST_URL"
UPSTASH_REST_TOKEN_ENV: Final[str] = "UPSTASH_REDIS_REST_TOKEN"


# ─── Exceptions ──────────────────────────────────────────────────────────────
class UpstashUnavailable(RuntimeError):
    """Raised when the Redis connection is unreachable or the server returned
    an error response.

    The gate catches this and converts it into a ``STATE_UNAVAILABLE`` denial.
    Keeping it a ``RuntimeError`` subclass means no caller is forced to import
    the type just to ``except`` it — but everything under the gate does, for
    precise handling.
    """


class UpstashConfigError(RuntimeError):
    """Raised at construction time when no usable connection URL is available.

    Separate from :class:`UpstashUnavailable` because the fix is different —
    this one requires an operator to set env vars, not a service recovery.
    """


# ─── Client ──────────────────────────────────────────────────────────────────
class UpstashClient:
    """Thin async wrapper around :class:`redis.asyncio.Redis`.

    Each method corresponds to one Redis command. The class is safe to share
    across requests because ``redis.asyncio.Redis`` manages its own async
    connection pool internally.

    Construction accepts either:

    * ``redis_url`` — a ``redis://`` or ``rediss://`` URL. We build a fresh
      :class:`redis.asyncio.Redis` from it and own its lifecycle.
    * ``redis_client`` — an already-constructed async Redis client (used by
      tests to inject :class:`fakeredis.FakeAsyncRedis`).
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        redis_client: Any | None = None,
    ) -> None:
        if redis_client is None and not redis_url:
            raise UpstashConfigError(
                "UpstashClient requires redis_url or redis_client. "
                f"Set ${REDIS_URL_ENV} (default {REDIS_URL_DEFAULT})."
            )
        self._owns_client = redis_client is None
        if redis_client is not None:
            self._redis = redis_client
        else:
            assert redis_url is not None  # narrowed by the guard above
            self._redis = redis_asyncio.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=UPSTASH_REST_TIMEOUT_SECONDS,
                socket_connect_timeout=UPSTASH_REST_TIMEOUT_SECONDS,
                retry_on_timeout=False,
                max_connections=32,
            )

    @classmethod
    def fromEnv(cls) -> "UpstashClient":
        """Build from environment variables.

        Resolution order (ADR-007):

        1. ``REDIS_URL`` — native ``redis://`` / ``rediss://`` URL.
        2. ``UPSTASH_REDIS_REST_URL`` + ``UPSTASH_REDIS_REST_TOKEN`` — if both
           are present, synthesize ``rediss://default:TOKEN@HOST:6379``. This
           is the legacy ADR-002 fallback and will only succeed against an
           Upstash plan that exposes a native Redis endpoint on the same host
           as the REST URL.
        3. ``REDIS_URL_DEFAULT`` (``redis://127.0.0.1:6379/0``) — local Redis.
        """
        url = os.environ.get(REDIS_URL_ENV, "").strip()
        if not url:
            rest_url = os.environ.get(UPSTASH_REST_URL_ENV, "").strip()
            rest_token = os.environ.get(UPSTASH_REST_TOKEN_ENV, "").strip()
            if rest_url and rest_token:
                host = urlparse(rest_url).hostname or ""
                if host:
                    url = f"rediss://default:{rest_token}@{host}:6379"
        if not url:
            url = REDIS_URL_DEFAULT
        return cls(redis_url=url)

    async def aclose(self) -> None:
        """Close the underlying Redis connection pool if we own it."""
        if self._owns_client:
            await self._redis.aclose()

    # ─── Internal retry helper ───────────────────────────────────────────────
    async def _call(self, op_name: str, coro_factory: Any) -> Any:
        """Run a redis-py awaitable, mapping errors to :class:`UpstashUnavailable`.

        Retries on :class:`ConnectionError` / :class:`TimeoutError` up to
        :data:`UPSTASH_MAX_RETRIES` additional times. Application-level
        errors (``WRONGTYPE``, invalid args) are :class:`RedisError` subclasses
        and are surfaced immediately without retry.
        """
        last_exc: Exception | None = None
        for attempt in range(UPSTASH_MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except (_RedisTimeoutError, _RedisConnectionError) as exc:
                last_exc = exc
                if attempt == UPSTASH_MAX_RETRIES:
                    break
                continue
            except RedisError as exc:
                raise UpstashUnavailable(f"{op_name} error: {exc}") from exc
        raise UpstashUnavailable(
            f"{op_name} unreachable after {UPSTASH_MAX_RETRIES + 1} attempts: "
            f"{last_exc!r}"
        )

    # ─── String / scalar commands ────────────────────────────────────────────
    async def get(self, key: str) -> str | None:
        """``GET key``. Returns ``None`` if the key does not exist."""
        result = await self._call("GET", lambda: self._redis.get(key))
        return None if result is None else str(result)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        """``SET key value [EX seconds] [NX]``.

        Returns ``True`` if the value was set. With ``nx=True`` and an
        existing key, Redis returns nil — we surface that as ``False``.
        """
        # redis-py expects nx=True/False; pass through only when asked.
        async def run() -> Any:
            if nx:
                return await self._redis.set(key, value, ex=ex, nx=True)
            return await self._redis.set(key, value, ex=ex)

        result = await self._call("SET", run)
        return bool(result is True)

    async def incrbyfloat(self, key: str, amount: float) -> float:
        """``INCRBYFLOAT key amount``. Returns the new value as a float."""
        result = await self._call(
            "INCRBYFLOAT", lambda: self._redis.incrbyfloat(key, amount)
        )
        return float(result)

    async def expire(self, key: str, seconds: int) -> bool:
        """``EXPIRE key seconds``. Returns ``True`` if the TTL was applied."""
        result = await self._call("EXPIRE", lambda: self._redis.expire(key, seconds))
        return bool(result)

    async def expireat(self, key: str, ts: int) -> bool:
        """``EXPIREAT key unix-ts``. Returns ``True`` if applied."""
        result = await self._call("EXPIREAT", lambda: self._redis.expireat(key, ts))
        return bool(result)

    async def delete(self, key: str) -> int:
        """``DEL key``. Returns the number of keys removed (0 or 1)."""
        result = await self._call("DEL", lambda: self._redis.delete(key))
        return int(result or 0)

    async def exists(self, key: str) -> bool:
        """``EXISTS key``. Returns ``True`` if the key exists."""
        result = await self._call("EXISTS", lambda: self._redis.exists(key))
        return bool(result)

    # ─── Hash commands ───────────────────────────────────────────────────────
    async def hset(self, key: str, mapping: dict[str, Any]) -> int:
        """``HSET key f1 v1 f2 v2 ...``. Returns the number of new fields."""
        if not mapping:
            return 0
        coerced = {str(k): str(v) for k, v in mapping.items()}
        result = await self._call(
            "HSET", lambda: self._redis.hset(key, mapping=coerced)
        )
        return int(result or 0)

    async def hgetall(self, key: str) -> dict[str, str]:
        """``HGETALL key`` as a plain ``dict[str, str]``."""
        result = await self._call("HGETALL", lambda: self._redis.hgetall(key))
        if not result:
            return {}
        return {str(k): str(v) for k, v in dict(result).items()}

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        """``HINCRBY key field amount``."""
        result = await self._call(
            "HINCRBY", lambda: self._redis.hincrby(key, field, amount)
        )
        return int(result)

    # ─── Set commands ────────────────────────────────────────────────────────
    async def sadd(self, key: str, value: str) -> int:
        """``SADD key value``. Returns 1 if added, 0 if already present."""
        result = await self._call("SADD", lambda: self._redis.sadd(key, value))
        return int(result or 0)

    async def smembers(self, key: str) -> _Set[str]:
        """``SMEMBERS key``. Returns the set of members; empty on miss."""
        result = await self._call("SMEMBERS", lambda: self._redis.smembers(key))
        if not result:
            return set()
        return {str(m) for m in result}

    async def srem(self, key: str, value: str) -> int:
        """``SREM key value``. Returns 1 if removed."""
        result = await self._call("SREM", lambda: self._redis.srem(key, value))
        return int(result or 0)

    # ─── List commands ───────────────────────────────────────────────────────
    async def lpush(self, key: str, value: str) -> int:
        """``LPUSH key value``. Returns the new list length."""
        result = await self._call("LPUSH", lambda: self._redis.lpush(key, value))
        return int(result or 0)

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        """``LTRIM key start stop``."""
        result = await self._call(
            "LTRIM", lambda: self._redis.ltrim(key, start, stop)
        )
        return bool(result)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        """``LRANGE key start stop``."""
        result = await self._call(
            "LRANGE", lambda: self._redis.lrange(key, start, stop)
        )
        if not result:
            return []
        return [str(v) for v in result]

    # ─── Pipeline ────────────────────────────────────────────────────────────
    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        """Execute multiple commands in a single round-trip.

        Returns one entry per command, in order. Any application-level error
        on any command raises :class:`UpstashUnavailable`.
        """
        if not commands:
            return []

        async def run() -> list[Any]:
            async with self._redis.pipeline(transaction=False) as pipe:
                for cmd in commands:
                    pipe.execute_command(*cmd)
                raw = await pipe.execute(raise_on_error=True)
            return list(raw)

        result: list[Any] = await self._call("PIPELINE", run)
        return result
