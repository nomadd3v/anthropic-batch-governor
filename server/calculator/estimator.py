"""Input-token estimation.

Two-tier strategy: call Anthropic's ``messages.count_tokens()`` when we can
(ground truth) and fall back to a cheap character heuristic when the API is
disabled or errors. Every result tags its ``source`` so downstream cost
projections can flag heuristic-sourced estimates as lower-confidence.
"""

from __future__ import annotations

import math
from typing import Any, Final, Literal

from pydantic import BaseModel, Field


# ─── Heuristic Constants ─────────────────────────────────────────────────────
# BUSINESS RULE: Claude's tokenizer averages ~3.5 characters per token across
# English text, code, and JSON. The heuristic only fires when
# ``count_tokens`` is unavailable; its job is to be "close enough to flag a
# 200k-input prompt", not to be precise.
CHARS_PER_TOKEN_HEURISTIC: Final[float] = 3.5

# BUSINESS RULE: Vision tiles bill at roughly 1600 tokens/tile. Anthropic's
# actual tile accounting varies with image dimensions (tiles scale with the
# larger of width/height), but 1600 is the conservative mid-range we use
# when we can't call ``count_tokens``. If an image block supplies an explicit
# ``tile_count``, multiply it; otherwise assume one tile.
TOKENS_PER_IMAGE_TILE: Final[int] = 1600

EstimationSource = Literal["api", "heuristic"]


# ─── Return Schema ───────────────────────────────────────────────────────────
class InputEstimate(BaseModel):
    """Estimated input-token count with provenance.

    ``source == "api"`` means the number came from
    ``client.messages.count_tokens()`` and can be trusted to the token.
    ``source == "heuristic"`` means the character fallback fired — treat as
    a lower-bound rough cut and consider retrying with the API available.
    """

    tokens: int = Field(..., ge=0)
    source: EstimationSource
    error: str | None = None


# ─── Anthropic Client Adapter (injectable for tests) ─────────────────────────
class _SupportsCountTokens:
    """Protocol-lite: the calculator only needs ``messages.count_tokens()``.

    We don't import the Anthropic SDK at module import time — that keeps
    ``pricing.py`` / ``batch.py`` tests fast and makes the estimator usable
    in environments without the SDK. ``estimateInputTokens`` accepts any
    object that exposes a ``messages.count_tokens`` callable with the SDK's
    shape.
    """


def estimateInputTokens(
    model: str,
    system: str | list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    use_count_tokens: bool = True,
    anthropic_client: Any | None = None,
) -> InputEstimate:
    """Estimate prompt input tokens for a single request.

    Returns a tagged ``InputEstimate``. Side effects: issues exactly one
    Anthropic ``count_tokens`` call when ``use_count_tokens`` is true and a
    client is provided.

    Falls back to the character heuristic on any of:
    - ``use_count_tokens=False``
    - ``anthropic_client is None``
    - the SDK call raises any exception

    ``model`` is passed through to ``count_tokens`` verbatim; the heuristic
    path ignores it.
    """
    if use_count_tokens and anthropic_client is not None:
        # AI: calls Anthropic messages.count_tokens (no billing). On any error
        # we fall through to the heuristic and surface ``error`` in the
        # returned estimate so the caller can log it.
        try:
            response = anthropic_client.messages.count_tokens(
                model=model,
                system=system if system is not None else [],
                messages=messages,
                tools=tools if tools is not None else [],
            )
            return InputEstimate(
                tokens=int(response.input_tokens),
                source="api",
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — we want to swallow and log.
            return _runCharacterHeuristic(
                system=system,
                messages=messages,
                tools=tools,
                error=f"count_tokens failed: {exc}",
            )

    return _runCharacterHeuristic(
        system=system,
        messages=messages,
        tools=tools,
        error=None,
    )


# ─── Character Heuristic ─────────────────────────────────────────────────────
def _runCharacterHeuristic(
    system: str | list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    error: str | None,
) -> InputEstimate:
    """Estimate tokens by totalling character counts and image tiles.

    ``total_chars / 3.5`` rounded up, plus a fixed 1600 tokens per image
    tile. Not precise — documents whose prompts live mostly in
    ``tool_use`` / ``tool_result`` dicts will be under-counted relative to
    the API call, which is acceptable for a fallback path.
    """
    total_chars = 0
    total_image_tiles = 0

    for block in _iterTextAndImageBlocks(system):
        total_chars += block.chars
        total_image_tiles += block.tiles

    for message in messages:
        for block in _iterTextAndImageBlocks(message.get("content")):
            total_chars += block.chars
            total_image_tiles += block.tiles

    if tools is not None:
        for tool in tools:
            total_chars += _approxJsonChars(tool)

    char_tokens = math.ceil(total_chars / CHARS_PER_TOKEN_HEURISTIC)
    image_tokens = total_image_tiles * TOKENS_PER_IMAGE_TILE
    return InputEstimate(
        tokens=char_tokens + image_tokens,
        source="heuristic",
        error=error,
    )


class _BlockMeasurement(BaseModel):
    chars: int
    tiles: int


def _iterTextAndImageBlocks(
    content: Any,
) -> list[_BlockMeasurement]:
    """Walk a prompt ``content`` field and yield per-block char/tile counts.

    Accepts all three shapes the Anthropic SDK emits:
    - ``None``
    - a plain string
    - a list of content blocks (dicts with ``type``)
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [_BlockMeasurement(chars=len(content), tiles=0)]
    if not isinstance(content, list):
        # Unknown shape — treat as its JSON representation length.
        return [_BlockMeasurement(chars=_approxJsonChars(content), tiles=0)]

    measurements: list[_BlockMeasurement] = []
    for block in content:
        if not isinstance(block, dict):
            measurements.append(
                _BlockMeasurement(chars=_approxJsonChars(block), tiles=0)
            )
            continue
        block_type = block.get("type")
        if block_type == "text":
            measurements.append(
                _BlockMeasurement(chars=len(str(block.get("text", ""))), tiles=0)
            )
        elif block_type == "image":
            tile_count = int(block.get("tile_count", 1))
            measurements.append(_BlockMeasurement(chars=0, tiles=tile_count))
        else:
            # tool_use / tool_result / unknown — count the JSON length as chars.
            measurements.append(
                _BlockMeasurement(chars=_approxJsonChars(block), tiles=0)
            )
    return measurements


def _approxJsonChars(value: Any) -> int:
    """Cheap char-length proxy for arbitrary JSON-serialisable values.

    We avoid importing ``json`` because ``repr`` is a sufficient stand-in for
    the heuristic and is never called on the API-success path.
    """
    return len(repr(value))
