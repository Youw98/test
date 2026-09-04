"""Dataset registry, local cache with provenance, and offline synthetic fallbacks."""

from kasflex.data.cache import CacheEntry, DataCache
from kasflex.data.registry import DATASETS, DatasetRef
from kasflex.data.synthetic import SyntheticDay, synthetic_day

__all__ = ["DATASETS", "CacheEntry", "DataCache", "DatasetRef", "SyntheticDay", "synthetic_day"]
