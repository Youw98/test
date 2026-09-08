"""Local data cache with recorded provenance.

Every external series KasFlex uses is downloaded once and then read from disk
forever after. Two requirements meet here: no demonstration may depend on a live
API call (R30), and every published figure must be traceable to the data it came
from (R34). A cache entry therefore stores not only the bytes but the source URL,
the retrieval date, the licence and a SHA-256 checksum.

Parquet is used when pyarrow is available and CSV otherwise; the manifest records
which, so a cache written on one machine is readable on another.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

MANIFEST_NAME = "MANIFEST.json"


@dataclass(frozen=True)
class CacheEntry:
    """Provenance record for one cached series."""

    key: str
    filename: str
    format: str
    sha256: str
    rows: int
    columns: list[str]
    source: str
    licence: str
    retrieved_on: str
    dataset_key: str = ""
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DataCache:
    """A directory of cached series plus a manifest describing where each came from."""

    def __init__(self, root: str | Path = "data/cache") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / MANIFEST_NAME

    # -- manifest ---------------------------------------------------------
    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _save_manifest(self, manifest: dict[str, dict[str, Any]]) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def entries(self) -> dict[str, CacheEntry]:
        return {k: CacheEntry(**v) for k, v in self._load_manifest().items()}

    def has(self, key: str) -> bool:
        entry = self._load_manifest().get(key)
        return entry is not None and (self.root / entry["filename"]).exists()

    # -- read / write -----------------------------------------------------
    def put(
        self,
        key: str,
        rows: list[dict[str, Any]],
        *,
        source: str,
        licence: str,
        dataset_key: str = "",
        notes: str = "",
        retrieved_on: str | None = None,
        prefer_parquet: bool = True,
    ) -> CacheEntry:
        """Write ``rows`` to the cache and record how they were obtained.

        Args:
            key: Cache key, e.g. ``"entsoe_da_2023-01-15"``.
            rows: Records to store. All records must share the same keys.
            source: URL or DOI the data came from.
            licence: Licence under which it was obtained.
            dataset_key: Matching key in :data:`kasflex.data.registry.DATASETS`.
            prefer_parquet: Use parquet when pyarrow is installed.

        Returns:
            The :class:`CacheEntry` written to the manifest.
        """
        if not rows:
            raise ValueError(f"refusing to cache an empty series for key {key!r}")
        columns = list(rows[0].keys())

        fmt = "csv"
        if prefer_parquet:
            try:
                import pyarrow as pa  # noqa: PLC0415
                import pyarrow.parquet as pq  # noqa: PLC0415

                fmt = "parquet"
                path = self.root / f"{key}.parquet"
                pq.write_table(pa.Table.from_pylist(rows), path)
            except ImportError:
                fmt = "csv"

        if fmt == "csv":
            path = self.root / f"{key}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)

        entry = CacheEntry(
            key=key,
            filename=path.name,
            format=fmt,
            sha256=sha256_file(path),
            rows=len(rows),
            columns=columns,
            source=source,
            licence=licence,
            retrieved_on=retrieved_on or date.today().isoformat(),
            dataset_key=dataset_key,
            notes=notes,
        )
        manifest = self._load_manifest()
        manifest[key] = asdict(entry)
        self._save_manifest(manifest)
        return entry

    def get(self, key: str, verify: bool = True) -> list[dict[str, Any]]:
        """Read a cached series back.

        Args:
            key: Cache key.
            verify: Re-check the stored SHA-256. A mismatch means the file changed
                underneath the manifest, which invalidates provenance, so it raises
                rather than returning data that cannot be cited.

        Raises:
            KeyError: if the key is not in the manifest.
            FileNotFoundError: if the manifest names a file that is gone.
            ValueError: if the checksum does not match.
        """
        manifest = self._load_manifest()
        if key not in manifest:
            raise KeyError(f"{key!r} is not in the cache manifest. Available: {sorted(manifest)}")
        entry = CacheEntry(**manifest[key])
        path = self.root / entry.filename
        if not path.exists():
            raise FileNotFoundError(
                f"cache manifest lists {entry.filename} for key {key!r}, but the file is missing"
            )
        if verify:
            actual = sha256_file(path)
            if actual != entry.sha256:
                raise ValueError(
                    f"checksum mismatch for cached series {key!r}: manifest says "
                    f"{entry.sha256[:12]}..., file is {actual[:12]}.... The cached data no "
                    f"longer matches its provenance record; re-fetch it."
                )

        if entry.format == "parquet":
            import pyarrow.parquet as pq  # noqa: PLC0415

            return pq.read_table(path).to_pylist()
        with path.open(newline="", encoding="utf-8") as fh:
            return [{k: _coerce(v) for k, v in row.items()} for row in csv.DictReader(fh)]


def _coerce(value: str) -> Any:
    """CSV has no types; restore numbers so cached and live paths behave alike."""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return value
