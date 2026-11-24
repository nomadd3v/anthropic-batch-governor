"""Python client for the batch-governor token calculator.

Drop-in import:

    from calc_tokens import CalcTokensClient

    client = CalcTokensClient()
    estimate = client.estimateInputTokens(
        model="claude-haiku-4-5-20251001",
        system=None,
        messages=[{"role": "user", "content": "hi"}],
    )
"""

from __future__ import annotations

from .client import (
    CalcTokensClient,
    DEFAULT_GOVERNOR_BASE_URL,
)
from .types import (
    BatchCostProjectionDict,
    BatchRequestSpecDict,
    CacheMode,
    CalibrationRecordDict,
    CalibrationSource,
    CostBreakdownDict,
    CostProjectionDict,
    EstimationSource,
    InputEstimateDict,
    ModelPricingDict,
    OutputProjectionDict,
)

__all__ = [
    "CalcTokensClient",
    "DEFAULT_GOVERNOR_BASE_URL",
    "BatchCostProjectionDict",
    "BatchRequestSpecDict",
    "CacheMode",
    "CalibrationRecordDict",
    "CalibrationSource",
    "CostBreakdownDict",
    "CostProjectionDict",
    "EstimationSource",
    "InputEstimateDict",
    "ModelPricingDict",
    "OutputProjectionDict",
]
