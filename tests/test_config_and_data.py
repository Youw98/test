"""Configuration strictness and data provenance."""

from __future__ import annotations

import pytest

from kasflex.config import ConfigError, ScenarioConfig
from kasflex.data.cache import DataCache
from kasflex.data.registry import DATASETS, as_markdown_table


def test_scenario_yaml_loads():
    config = ScenarioConfig.from_yaml("configs/scenario_westland_winter.yaml")
    assert config.name == "westland-winter"
    assert config.hub.floor_area_m2 == 50_000
    assert config.hub.contract.limits_at(17) == (3000.0, 1000.0)
    assert config.hub.contract.limits_at(12) == (6000, 4000)


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        ScenarioConfig.from_dict({"name": "x", "date": "d", "plannr": "rule-based"})


def test_unknown_nested_key_is_rejected():
    """A misspelled limit would silently change every result in a study."""
    with pytest.raises(ConfigError, match="unknown key"):
        ScenarioConfig.from_dict({"name": "x", "date": "d",
                                  "hub": {"battery": {"capacity_kWh": 100}}})


def test_required_keys():
    with pytest.raises(ConfigError, match="required"):
        ScenarioConfig.from_dict({"name": "x"})


# --- provenance ------------------------------------------------------------


def test_cache_records_provenance(tmp_path):
    cache = DataCache(tmp_path)
    entry = cache.put(
        "prices",
        [{"hour": h, "price": 0.1} for h in range(24)],
        source="https://transparency.entsoe.eu/",
        licence="ENTSO-E terms",
        dataset_key="entsoe_da",
    )
    assert entry.rows == 24
    assert entry.sha256
    assert entry.retrieved_on
    assert cache.entries()["prices"].source == "https://transparency.entsoe.eu/"
    assert cache.get("prices")[0]["hour"] == 0


def test_cache_detects_tampering(tmp_path):
    """Data that no longer matches its provenance record must not be citable."""
    cache = DataCache(tmp_path)
    cache.put("s", [{"a": 1}], source="x", licence="y")
    path = tmp_path / cache.entries()["s"].filename
    path.write_text(path.read_text() + "\n999\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        cache.get("s")


def test_cache_refuses_empty_series(tmp_path):
    with pytest.raises(ValueError, match="empty series"):
        DataCache(tmp_path).put("empty", [], source="x", licence="y")


def test_missing_key_names_what_is_available(tmp_path):
    cache = DataCache(tmp_path)
    cache.put("present", [{"a": 1}], source="x", licence="y")
    with pytest.raises(KeyError, match="present"):
        cache.get("absent")


def test_every_dataset_has_a_source_and_licence():
    for ref in DATASETS.values():
        assert ref.source.startswith("http"), ref.key
        assert ref.licence, ref.key
        assert ref.access, ref.key
        assert ref.verified_on, ref.key


def test_primary_dataset_is_the_second_agc_edition():
    """D1 is the correct choice: it carries the energy measurements and a human benchmark."""
    agc = DATASETS["agc2"]
    assert "10.4121/uuid:88d22c60" in agc.source
    assert agc.phase == "mvp"


def test_markdown_table_renders_every_dataset():
    table = as_markdown_table()
    for key in DATASETS:
        assert f"`{key}`" in table
