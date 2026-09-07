"""Dataset registry, local cache with provenance, and offline synthetic fallbacks."""

from kasflex.data.cache import CacheEntry, DataCache
from kasflex.data.registry import DATASETS, DatasetRef
from kasflex.data.synthetic import (
    SyntheticDay,
    SyntheticHistory,
    synthetic_day,
    synthetic_history,
)

__all__ = [
    "DATASETS",
    "CacheEntry",
    "DataCache",
    "DatasetRef",
    "SyntheticDay",
    "SyntheticHistory",
    "synthetic_day",
    "synthetic_history",
]
