"""Tests for :class:`server.upstash.UpstashClient`.

The governor now speaks native Redis RESP (ADR-007). Unit tests use
:class:`fakeredis.FakeAsyncRedis` as an in-memory stand-in so they remain
hermetic. Each test asserts either that the public surface matches a plain
Redis server's semantics, or that failure-path exceptions are mapped to
:class:`UpstashUnavailable`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import fakeredis
import pytest
from redis.exceptions import (
    ConnectionError as _RedisConnectionError,
)
from redis.exceptions import (
    ResponseError,
    TimeoutError as _RedisTimeoutError,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.upstash import (  # noqa: E402
    UPSTASH_MAX_RETRIES,
    REDIS_URL_DEFAULT,
    REDIS_URL_ENV,
    UPSTASH_REST_TOKEN_ENV,
    UPSTASH_REST_URL_ENV,
    UpstashClient,
    UpstashConfigError,
    UpstashUnavailable,
)


def _buildClient() -> UpstashClient:
    """Fresh in-memory UpstashClient backed by fakeredis."""
    fake = fakeredis.FakeAsyncRedis(decode_responses=True)
    return UpstashClient(redis_client=fake)


# ─── Construction ────────────────────────────────────────────────────────────
def test_missing_url_and_client_raises_config_error() -> None:
    with pytest.raises(UpstashConfigError):
        UpstashClient()


def test_from_env_prefers_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _fake_from_url(url: str, **_: Any) -> Any:
        captured["url"] = url
        return fakeredis.FakeAsyncRedis(decode_responses=True)

    monkeypatch.setenv(REDIS_URL_ENV, "redis://example:6379/3")
    monkeypatch.delenv(UPSTASH_REST_URL_ENV, raising=False)
    monkeypatch.delenv(UPSTASH_REST_TOKEN_ENV, raising=False)
    with patch("server.upstash.redis_asyncio.Redis.from_url", _fake_from_url):
        UpstashClient.fromEnv()
    assert captured["url"] == "redis://example:6379/3"


def test_from_env_synthesizes_rediss_url_from_upstash_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake_from_url(url: str, **_: Any) -> Any:
        captured["url"] = url
        return fakeredis.FakeAsyncRedis(decode_responses=True)

    monkeypatch.delenv(REDIS_URL_ENV, raising=False)
    monkeypatch.setenv(UPSTASH_REST_URL_ENV, "https://example-host.upstash.io")
    monkeypatch.setenv(UPSTASH_REST_TOKEN_ENV, "sekret")
    with patch("server.upstash.redis_asyncio.Redis.from_url", _fake_from_url):
        UpstashClient.fromEnv()
    assert captured["url"] == (
        "rediss://default:sekret@example-host.upstash.io:6379"
    )


def test_from_env_falls_back_to_loopback_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake_from_url(url: str, **_: Any) -> Any:
        captured["url"] = url
        return fakeredis.FakeAsyncRedis(decode_responses=True)

    monkeypatch.delenv(REDIS_URL_ENV, raising=False)
    monkeypatch.delenv(UPSTASH_REST_URL_ENV, raising=False)
    monkeypatch.delenv(UPSTASH_REST_TOKEN_ENV, raising=False)
    with patch("server.upstash.redis_asyncio.Redis.from_url", _fake_from_url):
        UpstashClient.fromEnv()
    assert captured["url"] == REDIS_URL_DEFAULT


# ─── String / scalar ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_returns_value_on_hit() -> None:
    client = _buildClient()
    try:
        await client._redis.set("some-key", "hello")
        assert await client.get("some-key") == "hello"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_returns_none_when_key_missing() -> None:
    client = _buildClient()
    try:
        assert await client.get("missing") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_set_with_ex_and_nx_returns_true() -> None:
    client = _buildClient()
    try:
        ok = await client.set("k", "v", ex=60, nx=True)
    finally:
        await client.aclose()
    assert ok is True


@pytest.mark.asyncio
async def test_set_nx_returns_false_when_key_exists() -> None:
    client = _buildClient()
    try:
        await client._redis.set("k", "already")
        ok = await client.set("k", "v", nx=True)
    finally:
        await client.aclose()
    assert ok is False


@pytest.mark.asyncio
async def test_incrbyfloat_returns_new_value() -> None:
    client = _buildClient()
    try:
        await client._redis.set("spend:daily", "10.00")
        value = await client.incrbyfloat("spend:daily", 2.34)
    finally:
        await client.aclose()
    assert value == pytest.approx(12.34)


# ─── Hash ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hset_and_hgetall_roundtrip() -> None:
    client = _buildClient()
    try:
        n = await client.hset("k", {"f1": "v1", "f2": "v2"})
        got = await client.hgetall("k")
    finally:
        await client.aclose()
    assert n == 2
    assert got == {"f1": "v1", "f2": "v2"}


@pytest.mark.asyncio
async def test_hgetall_empty_returns_empty_dict() -> None:
    client = _buildClient()
    try:
        assert await client.hgetall("never-set") == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hincrby_accumulates() -> None:
    client = _buildClient()
    try:
        assert await client.hincrby("counts", "f", 3) == 3
        assert await client.hincrby("counts", "f", 4) == 7
    finally:
        await client.aclose()


# ─── Sets + lists ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sadd_smembers_srem_roundtrip() -> None:
    client = _buildClient()
    try:
        added = await client.sadd("s", "a")
        await client.sadd("s", "b")
        members = await client.smembers("s")
        removed = await client.srem("s", "a")
    finally:
        await client.aclose()

    assert added == 1
    assert members == {"a", "b"}
    assert removed == 1


@pytest.mark.asyncio
async def test_list_commands_emit_and_parse() -> None:
    client = _buildClient()
    try:
        length = await client.lpush("log", "z")
        await client.lpush("log", "y")
        await client.lpush("log", "x")
        assert length == 1
        trimmed = await client.ltrim("log", 0, 99)
        rows = await client.lrange("log", 0, -1)
    finally:
        await client.aclose()

    assert trimmed is True
    # LPUSH prepends, so insertion order z, y, x puts x at head.
    assert rows == ["x", "y", "z"]


# ─── Pipelines ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pipeline_returns_one_result_per_command() -> None:
    client = _buildClient()
    try:
        results = await client.pipeline([
            ["SET", "k", "v"],
            ["EXPIRE", "k", 60],
            ["INCRBYFLOAT", "spend", 0.50],
        ])
    finally:
        await client.aclose()
    # Order and count preserved; SET returns "OK"/True, EXPIRE returns int,
    # INCRBYFLOAT returns a bytes-free numeric string when decode_responses is on.
    assert len(results) == 3
    assert float(results[2]) == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_pipeline_empty_returns_empty_list() -> None:
    client = _buildClient()
    try:
        assert await client.pipeline([]) == []
    finally:
        await client.aclose()


# ─── Failure paths ───────────────────────────────────────────────────────────
class _FlakyRedis:
    """Wrap a fakeredis client so every call raises a given exception.

    Used to exercise the retry + mapping logic in ``UpstashClient._call``
    without a real network.
    """

    def __init__(self, exc_factory: Any) -> None:
        self._exc_factory = exc_factory
        self.call_count = 0

    def __getattr__(self, name: str) -> Any:
        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            self.call_count += 1
            raise self._exc_factory()

        return _raise


@pytest.mark.asyncio
async def test_connection_error_retries_then_raises() -> None:
    flaky = _FlakyRedis(lambda: _RedisConnectionError("boom"))
    client = UpstashClient(redis_client=flaky)
    with pytest.raises(UpstashUnavailable):
        await client.get("k")
    # Initial attempt + UPSTASH_MAX_RETRIES retries.
    assert flaky.call_count == UPSTASH_MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_timeout_retries_then_raises() -> None:
    flaky = _FlakyRedis(lambda: _RedisTimeoutError("slow"))
    client = UpstashClient(redis_client=flaky)
    with pytest.raises(UpstashUnavailable):
        await client.get("k")
    assert flaky.call_count == UPSTASH_MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_application_error_is_not_retried() -> None:
    flaky = _FlakyRedis(lambda: ResponseError("WRONGTYPE"))
    client = UpstashClient(redis_client=flaky)
    with pytest.raises(UpstashUnavailable) as excinfo:
        await client.get("k")
    assert "WRONGTYPE" in str(excinfo.value)
    assert flaky.call_count == 1


@pytest.mark.asyncio
async def test_wrongtype_from_real_fakeredis() -> None:
    """Sanity: a real-shape application error (from fakeredis, not a mock)
    surfaces as :class:`UpstashUnavailable`."""
    client = _buildClient()
    try:
        await client._redis.set("k", "string-value")
        with pytest.raises(UpstashUnavailable):
            # Using a HASH command on a STRING key triggers WRONGTYPE.
            await client.hgetall("k")
    finally:
        await client.aclose()
