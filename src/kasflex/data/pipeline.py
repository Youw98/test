"""The daily job: fetch what is missing, cache it, run the day, record the result.

Written to survive being run by cron at 14:00 every afternoon and never looked at
again. Four properties make that possible:

**Cache-first.** Anything already cached is never re-fetched. Re-running the job
for the same day is free and produces the same result.

**Offline-safe.** If the network is down but the day is cached, the run proceeds. A
scheduled job that fails because a public API blipped is a job that will be switched
off within a fortnight.

**Loud only when it matters.** Missing data that the cache cannot cover raises with
a message naming the day and what was missing. Everything else is a warning.

**Provenance on every row.** Each cached series records its source, licence,
retrieval date and checksum, so a result computed months later can still be traced
to what produced it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta
from pathlib import Path
from typing import Any

from kasflex.data.cache import DataCache
from kasflex.data.sources import (
    ENTSOE_META,
    OPENMETEO_ARCHIVE_META,
    OPENMETEO_FORECAST_META,
    FetchError,
    fetch_entsoe_day_ahead,
    fetch_openmeteo,
)
from kasflex.energy.dispatch import HourlyConditions

HOURS = 24


@dataclass(frozen=True)
class DayData:
    """One day's external inputs, assembled from cache and/or the network."""

    date: str
    prices_eur_kwh: tuple[float, ...]
    forecast_temp_c: tuple[float, ...]
    forecast_irradiance_w_m2: tuple[float, ...]
    actual_temp_c: tuple[float, ...] | None = None
    actual_irradiance_w_m2: tuple[float, ...] | None = None
    gas_price_eur_kwh: float = 0.035
    sources: dict[str, str] = field(default_factory=dict)
    """Per-series provenance: ``"cache"`` or ``"network"``."""

    @property
    def actuals_available(self) -> bool:
        """False when the archive has not caught up with the day yet.

        A day planned this afternoon has no realised weather, so the run is scored
        against the forecast and the result record says so. Treating that as if it
        were a real out-of-sample score would quietly turn every forward-looking run
        into an oracle result.
        """
        return self.actual_temp_c is not None

    def conditions(self) -> tuple[tuple[HourlyConditions, ...], tuple[HourlyConditions, ...]]:
        """Build the forecast and actual condition series.

        Heat and CO2 demand are left at zero: they come from the greenhouse model in
        :func:`kasflex.run.run_scenario`, which is the only thing that knows how this
        particular greenhouse responds to this weather.
        """
        forecast = tuple(
            HourlyConditions(
                hour=h,
                irradiance_w_m2=self.forecast_irradiance_w_m2[h],
                outdoor_temp_c=self.forecast_temp_c[h],
                power_price_eur_kwh=self.prices_eur_kwh[h],
                gas_price_eur_kwh=self.gas_price_eur_kwh,
            )
            for h in range(HOURS)
        )
        if not self.actuals_available:
            return forecast, forecast
        actual = tuple(
            HourlyConditions(
                hour=h,
                irradiance_w_m2=self.actual_irradiance_w_m2[h],
                outdoor_temp_c=self.actual_temp_c[h],
                power_price_eur_kwh=self.prices_eur_kwh[h],
                gas_price_eur_kwh=self.gas_price_eur_kwh,
            )
            for h in range(HOURS)
        )
        return forecast, actual


def _column(rows: list[dict[str, Any]], name: str) -> tuple[float, ...]:
    ordered = sorted(rows, key=lambda r: int(r["hour"]))
    if len(ordered) != HOURS:
        raise FetchError(f"expected {HOURS} rows for column {name!r}, got {len(ordered)}")
    return tuple(float(r[name]) for r in ordered)


def _cached_or_fetch(
    cache: DataCache,
    key: str,
    fetcher,
    meta,
    *,
    allow_network: bool,
    required: bool,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return cached rows if present, else fetch and cache them.

    Returns:
        ``(rows, origin)`` where origin is ``"cache"``, ``"network"`` or
        ``"missing"``. ``rows`` is None only when the series is optional and could
        not be obtained.

    Raises:
        FetchError: when a required series is available from neither source.
    """
    if cache.has(key):
        return cache.get(key), "cache"

    if not allow_network:
        if required:
            raise FetchError(
                f"{key!r} is not cached and network access is disabled. Run "
                f"`kasflex fetch` while online, or pass --allow-network."
            )
        return None, "missing"

    try:
        rows = fetcher()
    except FetchError:
        if required:
            raise
        return None, "missing"

    cache.put(
        key,
        rows,
        source=meta.source,
        licence=meta.licence,
        dataset_key=meta.dataset_key,
    )
    return rows, "network"


def ensure_day(
    day: Date,
    *,
    cache: DataCache | None = None,
    latitude: float,
    longitude: float,
    gas_price_eur_kwh: float = 0.035,
    entsoe_zone: str | None = None,
    api_key: str | None = None,
    allow_network: bool = True,
    want_actuals: bool = True,
) -> DayData:
    """Assemble one day's inputs, fetching only what the cache lacks.

    Args:
        day: The local calendar day.
        cache: Where series are stored. Defaults to ``data/cache``.
        latitude, longitude: Site location for the weather request.
        gas_price_eur_kwh: TTF gas price. There is no free public API for this, so
            it is a configured value; update it when the market moves materially.
        api_key: ENTSO-E token. Defaults to ``ENTSOE_API_KEY`` in the environment.
        allow_network: When False, use only what is already cached.
        want_actuals: Try to fetch realised weather from the archive. Harmless to
            leave on: a day that has not happened yet simply has none.

    Raises:
        FetchError: if prices or forecast weather can be obtained from neither the
            cache nor the network.
    """
    cache = cache or DataCache()
    key = api_key if api_key is not None else os.environ.get("ENTSOE_API_KEY", "")
    site = f"{latitude:.3f}_{longitude:.3f}"
    iso = day.isoformat()
    sources: dict[str, str] = {}

    zone_kwargs = {"zone": entsoe_zone} if entsoe_zone else {}
    price_rows, sources["prices"] = _cached_or_fetch(
        cache,
        f"entsoe_da_{iso}",
        lambda: fetch_entsoe_day_ahead(day, key, **zone_kwargs),
        ENTSOE_META,
        allow_network=allow_network,
        required=True,
    )
    forecast_rows, sources["forecast_weather"] = _cached_or_fetch(
        cache,
        f"weather_forecast_{iso}_{site}",
        lambda: fetch_openmeteo(day, latitude=latitude, longitude=longitude, archive=False),
        OPENMETEO_FORECAST_META,
        allow_network=allow_network,
        required=True,
    )

    actual_rows = None
    sources["actual_weather"] = "not requested"
    if want_actuals:
        actual_rows, sources["actual_weather"] = _cached_or_fetch(
            cache,
            f"weather_actual_{iso}_{site}",
            lambda: fetch_openmeteo(day, latitude=latitude, longitude=longitude, archive=True),
            OPENMETEO_ARCHIVE_META,
            allow_network=allow_network,
            required=False,
        )

    return DayData(
        date=iso,
        prices_eur_kwh=_column(price_rows, "price_eur_kwh"),
        forecast_temp_c=_column(forecast_rows, "outdoor_temp_c"),
        forecast_irradiance_w_m2=_column(forecast_rows, "irradiance_w_m2"),
        actual_temp_c=_column(actual_rows, "outdoor_temp_c") if actual_rows else None,
        actual_irradiance_w_m2=_column(actual_rows, "irradiance_w_m2") if actual_rows else None,
        gas_price_eur_kwh=gas_price_eur_kwh,
        sources=sources,
    )


def cached_days(cache: DataCache, latitude: float, longitude: float) -> list[str]:
    """Dates for which both prices and forecast weather are cached, chronological."""
    site = f"{latitude:.3f}_{longitude:.3f}"
    entries = cache.entries()
    dates = {
        k.removeprefix("entsoe_da_") for k in entries if k.startswith("entsoe_da_")
    }
    have_weather = {
        k.removeprefix("weather_forecast_").removesuffix(f"_{site}")
        for k in entries
        if k.startswith("weather_forecast_") and k.endswith(site)
    }
    return sorted(dates & have_weather)


def run_daily(
    config,
    day: Date | None = None,
    *,
    cache: DataCache | None = None,
    allow_network: bool = True,
    results_path: str | Path = "results/daily.jsonl",
) -> dict[str, Any]:
    """Fetch, plan and record one day. The whole job, in one call.

    Args:
        config: A :class:`~kasflex.config.ScenarioConfig`.
        day: Day to plan. Defaults to tomorrow, which is what a job running this
            afternoon wants: day-ahead prices for tomorrow publish around 13:00 CET.
        allow_network: When False, run entirely from cache.
        results_path: JSON Lines file that one record is appended to.

    Returns:
        The result record that was appended.
    """
    from kasflex.experiment import build_greenhouse, build_planner
    from kasflex.oversight import AuditLog
    from kasflex.run import run_scenario

    target = day or (Date.today() + timedelta(days=1))
    data = ensure_day(
        target,
        cache=cache,
        latitude=config.latitude,
        longitude=config.longitude,
        gas_price_eur_kwh=config.gas_price_eur_kwh,
        entsoe_zone=config.entsoe_zone,
        allow_network=allow_network,
    )
    forecast, actual = data.conditions()

    result = run_scenario(
        scenario=f"{config.name}/daily",
        date=data.date,
        hub=config.hub,
        forecast=forecast,
        actual=actual,
        planner=build_planner(config.planner, config),
        greenhouse=build_greenhouse(config.greenhouse, config),
        checker_config=config.checker,
        audit_log=AuditLog(config.audit_path),
        brief=config.brief,
        seed=config.seed,
        provenance={
            "data_source": "live",
            "series": data.sources,
            "actuals_available": data.actuals_available,
        },
    )

    record = result.to_record()
    record["actuals_available"] = data.actuals_available
    record["series_origin"] = data.sources
    if not data.actuals_available:
        record["note"] = (
            "scored against the forecast: the weather archive has no realised data "
            "for this day yet. Re-run after the day has passed for an out-of-sample "
            "score."
        )

    path = Path(results_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return record


def build_history_from_cache(
    cache: DataCache,
    config,
    greenhouse,
    *,
    up_to: Date,
    max_days: int = 120,
):
    """Assemble a training history for the learned planner out of cached days.

    Returns whatever the cache actually holds, in order, ending before ``up_to``.
    The learned planner needs roughly three weeks before its lag features are
    usable; below that the caller should fall back to another planner rather than
    fit a model on nothing.
    """
    from kasflex.forecast.history import MeteredDay

    days = [d for d in cached_days(cache, config.latitude, config.longitude)
            if d < up_to.isoformat()][-max_days:]
    out = []
    for iso in days:
        data = ensure_day(
            Date.fromisoformat(iso),
            cache=cache,
            latitude=config.latitude,
            longitude=config.longitude,
            gas_price_eur_kwh=config.gas_price_eur_kwh,
            entsoe_zone=config.entsoe_zone,
            allow_network=False,
            want_actuals=True,
        )
        forecast, actual = data.conditions()
        area = config.hub.floor_area_m2
        from kasflex.intent import flat_plan

        plan = flat_plan(iso, heat_source="boiler", lighting_level=0.4, co2_source="liquid")
        realised = greenhouse.simulate_day(plan, actual, area)
        predicted = greenhouse.simulate_day(plan, forecast, area)
        out.append(
            MeteredDay(
                date=iso,
                forecast=tuple(
                    dataclasses.replace(
                        c,
                        heat_demand_kw=predicted.heat_demand_kw[i],
                        co2_demand_kg_h=predicted.co2_demand_kg_h[i],
                    )
                    for i, c in enumerate(forecast)
                ),
                actual=tuple(
                    dataclasses.replace(
                        c,
                        heat_demand_kw=realised.heat_demand_kw[i],
                        co2_demand_kg_h=realised.co2_demand_kg_h[i],
                    )
                    for i, c in enumerate(actual)
                ),
            )
        )
    return out
