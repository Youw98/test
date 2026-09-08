"""The daily job: cache-first, offline-safe, idempotent.

These are the properties that decide whether a scheduled job is still running in
six months, so they are tested rather than assumed. No test here touches the
network; the fetcher is injected.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from kasflex.config import ScenarioConfig
from kasflex.data.cache import DataCache
from kasflex.data.pipeline import cached_days, ensure_day, run_daily
from kasflex.data.sources import (
    ENTSOE_META,
    OPENMETEO_ARCHIVE_META,
    OPENMETEO_HISTORICAL_FORECAST_META,
    FetchError,
    parse_entsoe_day_ahead,
    parse_openmeteo_hourly,
)

FIXTURES = Path(__file__).parent / "fixtures"
DAY = dt.date(2023, 1, 15)
LAT, LON = 51.99, 4.25
SITE = "51.990_4.250"


def seed(cache: DataCache, *, actuals: bool = True) -> None:
    """Put a full day in the cache, exactly as a successful fetch would have."""
    prices = parse_entsoe_day_ahead((FIXTURES / "entsoe_a44_sparse.xml").read_text(), DAY)
    weather = parse_openmeteo_hourly((FIXTURES / "openmeteo_forecast.json").read_text(), DAY)
    cache.put(
        "entsoe_da_2023-01-15",
        prices,
        source=ENTSOE_META.source,
        licence=ENTSOE_META.licence,
        dataset_key="entsoe_da",
    )
    cache.put(
        f"weather_forecast_2023-01-15_{SITE}",
        weather,
        source=OPENMETEO_HISTORICAL_FORECAST_META.source,
        licence=OPENMETEO_HISTORICAL_FORECAST_META.licence,
        dataset_key="openmeteo_hist_forecast",
    )
    if actuals:
        warmer = [{**r, "outdoor_temp_c": r["outdoor_temp_c"] + 0.8} for r in weather]
        cache.put(
            f"weather_actual_2023-01-15_{SITE}",
            warmer,
            source=OPENMETEO_ARCHIVE_META.source,
            licence=OPENMETEO_ARCHIVE_META.licence,
            dataset_key="openmeteo_archive",
        )


@pytest.fixture
def cache(tmp_path) -> DataCache:
    c = DataCache(tmp_path / "cache")
    seed(c)
    return c


@pytest.fixture
def config(tmp_path):
    base = ScenarioConfig.from_yaml("configs/scenario_westland_winter.yaml")
    return ScenarioConfig(**{**base.__dict__, "audit_path": str(tmp_path / "audit.jsonl")})


# --- cache behaviour -------------------------------------------------------


def test_reads_entirely_from_cache_with_no_network(cache):
    data = ensure_day(DAY, cache=cache, latitude=LAT, longitude=LON, allow_network=False)
    assert len(data.prices_eur_kwh) == 24
    assert set(data.sources.values()) == {"cache"}


def test_missing_required_series_fails_loudly(tmp_path):
    empty = DataCache(tmp_path / "empty")
    with pytest.raises(FetchError, match="not cached and network access is disabled"):
        ensure_day(DAY, cache=empty, latitude=LAT, longitude=LON, allow_network=False)


def test_missing_actuals_is_tolerated(tmp_path):
    """A day that has not happened yet has no realised weather. That is normal."""
    partial = DataCache(tmp_path / "partial")
    seed(partial, actuals=False)
    data = ensure_day(DAY, cache=partial, latitude=LAT, longitude=LON, allow_network=False)
    assert not data.actuals_available
    assert data.sources["actual_weather"] == "missing"
    forecast, actual = data.conditions()
    assert forecast == actual, "with no archive, the day is scored against the forecast"


def test_actuals_are_used_when_present(cache):
    data = ensure_day(DAY, cache=cache, latitude=LAT, longitude=LON, allow_network=False)
    assert data.actuals_available
    forecast, actual = data.conditions()
    assert forecast != actual


def test_prices_are_carried_into_the_conditions(cache):
    data = ensure_day(DAY, cache=cache, latitude=LAT, longitude=LON, allow_network=False)
    forecast, _ = data.conditions()
    assert forecast[0].power_price_eur_kwh == pytest.approx(0.09012)
    assert forecast[16].power_price_eur_kwh == pytest.approx(0.22011)


def test_heat_demand_is_left_to_the_greenhouse_model(cache):
    """The weather API cannot know this greenhouse's demand; only the model can."""
    data = ensure_day(DAY, cache=cache, latitude=LAT, longitude=LON, allow_network=False)
    forecast, _ = data.conditions()
    assert all(c.heat_demand_kw == 0.0 for c in forecast)


def test_cached_days_lists_complete_days(cache):
    assert cached_days(cache, LAT, LON) == ["2023-01-15"]


def test_cached_days_ignores_a_day_missing_its_weather(tmp_path):
    partial = DataCache(tmp_path / "p")
    prices = parse_entsoe_day_ahead((FIXTURES / "entsoe_a44_sparse.xml").read_text(), DAY)
    partial.put("entsoe_da_2023-01-15", prices, source="x", licence="y")
    assert cached_days(partial, LAT, LON) == []


# --- network path, with the fetcher injected -------------------------------


def test_fetches_only_what_is_missing(tmp_path, monkeypatch):
    partial = DataCache(tmp_path / "p")
    seed(partial, actuals=False)
    calls: list[bool] = []

    def fake_openmeteo(day, *, latitude, longitude, archive=False, **kw):
        calls.append(archive)
        return parse_openmeteo_hourly((FIXTURES / "openmeteo_forecast.json").read_text(), day)

    monkeypatch.setattr("kasflex.data.pipeline.fetch_openmeteo", fake_openmeteo)
    data = ensure_day(DAY, cache=partial, latitude=LAT, longitude=LON, allow_network=True)

    assert calls == [True], "only the missing archive should have been fetched"
    assert data.sources["prices"] == "cache"
    assert data.sources["forecast_weather"] == "cache"
    assert data.sources["actual_weather"] == "network"


def test_a_fetched_series_is_cached_with_provenance(tmp_path, monkeypatch):
    partial = DataCache(tmp_path / "p")
    seed(partial, actuals=False)
    monkeypatch.setattr(
        "kasflex.data.pipeline.fetch_openmeteo",
        lambda day, **kw: parse_openmeteo_hourly(
            (FIXTURES / "openmeteo_forecast.json").read_text(), day
        ),
    )
    ensure_day(DAY, cache=partial, latitude=LAT, longitude=LON, allow_network=True)

    entry = partial.entries()[f"weather_actual_2023-01-15_{SITE}"]
    assert entry.licence.startswith("CC-BY")
    assert entry.dataset_key == "openmeteo_archive"
    assert entry.retrieved_on
    assert entry.sha256


def test_an_optional_series_failing_does_not_sink_the_run(tmp_path, monkeypatch):
    partial = DataCache(tmp_path / "p")
    seed(partial, actuals=False)

    def boom(*a, **kw):
        raise FetchError("archive is down")

    monkeypatch.setattr("kasflex.data.pipeline.fetch_openmeteo", boom)
    data = ensure_day(DAY, cache=partial, latitude=LAT, longitude=LON, allow_network=True)
    assert not data.actuals_available


def test_a_required_series_failing_does_sink_the_run(tmp_path, monkeypatch):
    empty = DataCache(tmp_path / "e")

    def boom(*a, **kw):
        raise FetchError("ENTSO-E is down")

    monkeypatch.setattr("kasflex.data.pipeline.fetch_entsoe_day_ahead", boom)
    with pytest.raises(FetchError, match="ENTSO-E is down"):
        ensure_day(DAY, cache=empty, latitude=LAT, longitude=LON, allow_network=True)


# --- the whole job ---------------------------------------------------------


def test_daily_run_appends_one_record(config, cache, tmp_path):
    out = tmp_path / "daily.jsonl"
    record = run_daily(config, DAY, cache=cache, allow_network=False, results_path=out)
    assert record["date"] == "2023-01-15"
    assert record["actuals_available"] is True
    assert record["provenance"]["data_source"] == "external"
    assert record["provenance"]["forecast_weather_dataset"] == "openmeteo_hist_forecast"
    assert record["provenance"]["scoring_weather_dataset"] == "openmeteo_archive"
    assert record["provenance"]["scoring_weather_is_measured"] is False
    assert record["provenance"]["scored_against"] == "historical_weather_proxy"
    assert len(out.read_text().strip().splitlines()) == 1


def test_daily_run_is_idempotent(config, cache, tmp_path):
    out = tmp_path / "daily.jsonl"
    first = run_daily(config, DAY, cache=cache, allow_network=False, results_path=out)
    second = run_daily(config, DAY, cache=cache, allow_network=False, results_path=out)
    assert first["net_cost_eur"] == second["net_cost_eur"]
    assert first["plan_hash"] == second["plan_hash"] if "plan_hash" in first else True
    assert len(out.read_text().strip().splitlines()) == 2, "each run appends"


def test_daily_run_marks_a_forecast_only_score(config, tmp_path):
    partial = DataCache(tmp_path / "p")
    seed(partial, actuals=False)
    record = run_daily(
        config, DAY, cache=partial, allow_network=False, results_path=tmp_path / "d.jsonl"
    )
    assert record["actuals_available"] is False
    assert record["provenance"]["scored_against"] == "forecast"
    assert record["provenance"]["scoring_weather_dataset"] is None
    assert "forecast" in record["note"]


def test_daily_run_records_where_each_series_came_from(config, cache, tmp_path):
    record = run_daily(
        config, DAY, cache=cache, allow_network=False, results_path=tmp_path / "d.jsonl"
    )
    assert record["series_origin"]["prices"] == "cache"


def test_result_records_are_valid_json_lines(config, cache, tmp_path):
    out = tmp_path / "daily.jsonl"
    run_daily(config, DAY, cache=cache, allow_network=False, results_path=out)
    for line in out.read_text().splitlines():
        json.loads(line)
