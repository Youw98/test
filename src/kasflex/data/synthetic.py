"""Deterministic synthetic day, for tests and for running with no data at all.

The requirements allow synthetic data "in rare cases". This is one: a new user must
be able to clone the repository and run a scenario before obtaining an ENTSO-E API
key or downloading anything, and the test suite must not touch the network.

Everything here is seeded and reproducible (R6). It is **not** a substitute for the
real series: any run using it is stamped ``synthetic`` in its result record, and
:func:`kasflex.run.run_scenario` refuses to mark such a run as validated.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from kasflex.energy.dispatch import HourlyConditions

# A recognisable Dutch winter day-ahead price shape: overnight trough, morning and
# evening peaks, midday dip from solar. Scaled by the level drawn per day.
SETPOINT_C = 18.5
"""Greenhouse balance-point temperature: below this, heating is needed."""

_PRICE_SHAPE = (
    0.62, 0.58, 0.55, 0.54, 0.57, 0.68, 0.86, 1.10,
    1.28, 1.12, 0.95, 0.86, 0.82, 0.83, 0.90, 1.05,
    1.30, 1.52, 1.60, 1.42, 1.18, 0.98, 0.82, 0.70,
)


@dataclass(frozen=True)
class SyntheticDay:
    """A synthetic forecast/actual pair for one day."""

    date: str
    seed: int
    forecast: tuple[HourlyConditions, ...]
    actual: tuple[HourlyConditions, ...]
    provenance: str = "synthetic"

    def __post_init__(self) -> None:
        if len(self.forecast) != 24 or len(self.actual) != 24:
            raise ValueError("a synthetic day must contain 24 forecast and 24 actual hours")


def synthetic_day(
    date: str,
    seed: int = 0,
    *,
    floor_area_m2: float = 50_000.0,
    winter: bool = True,
    forecast_error: bool = True,
    mean_temp_c: float | None = None,
) -> SyntheticDay:
    """Generate one deterministic synthetic day.

    Args:
        date: ISO date, used only as a label.
        seed: Random seed. The same seed always yields the same day (R6).
        floor_area_m2: Greenhouse area, used to scale the heat and CO2 demand.
        winter: Winter days have low irradiance and high heat demand; summer days
            the reverse.
        forecast_error: When True the forecast differs from the actual, as it does
            in reality. When False the two are identical, which is useful for
            isolating planner error from forecast error.
        mean_temp_c: Daily mean outdoor temperature. Drawn independently when None.
            :func:`synthetic_history` supplies it from an autocorrelated series so
            that consecutive days resemble each other, as real weather does.

    Returns:
        A :class:`SyntheticDay` whose ``forecast`` is what the planner may see and
        whose ``actual`` is what the run is evaluated against (R4).
    """
    rng = random.Random(seed)
    price_level = rng.uniform(0.06, 0.16)
    gas_price = rng.uniform(0.028, 0.055)
    peak_irradiance = rng.uniform(90.0, 210.0) if winter else rng.uniform(450.0, 780.0)
    area = floor_area_m2
    # Daily mean outdoor temperature, and how far the day swings around it.
    if mean_temp_c is None:
        mean_temp_c = rng.uniform(-1.0, 9.0) if winter else rng.uniform(13.0, 25.0)
    temp_swing_c = rng.uniform(2.0, 6.0)
    # Heat demand follows degree-hours below the greenhouse setpoint. Expressing it
    # this way rather than as an arbitrary scale is what makes the series learnable
    # from weather, which is the whole point of having a forecaster.
    u_value_kw_m2_k = rng.uniform(0.0050, 0.0072)

    forecast: list[HourlyConditions] = []
    actual: list[HourlyConditions] = []

    for hour in range(24):
        # Daylight window: a raised cosine centred on solar noon.
        day_frac = max(0.0, math.sin(math.pi * (hour - 7.0) / 10.0)) if 7 <= hour <= 17 else 0.0
        irradiance = peak_irradiance * day_frac
        # Coldest just before dawn, warmest mid-afternoon.
        outdoor_c = mean_temp_c + temp_swing_c * math.cos(math.pi * (hour - 15.0) / 12.0)
        degree_hours = max(0.0, SETPOINT_C - outdoor_c)
        solar_gain_k = 0.0055 * irradiance
        heat_demand = area * u_value_kw_m2_k * max(0.0, degree_hours - solar_gain_k)
        co2_demand = area * 0.0012 * day_frac

        base = HourlyConditions(
            hour=hour,
            heat_demand_kw=max(0.0, heat_demand),
            co2_demand_kg_h=max(0.0, co2_demand),
            irradiance_w_m2=irradiance,
            outdoor_temp_c=round(outdoor_c, 3),
            power_price_eur_kwh=round(price_level * _PRICE_SHAPE[hour], 5),
            gas_price_eur_kwh=round(gas_price, 5),
        )
        forecast.append(base)

        if not forecast_error:
            actual.append(base)
            continue
        # Realised conditions drift from the forecast: cloud cover and temperature
        # error dominate, and the day-ahead price is known exactly by gate closure.
        # Realised conditions drift from the forecast: the temperature error is the
        # one that matters, because heat demand follows from it. Demand is
        # recomputed from the realised temperature rather than perturbed
        # independently, so the actual series stays physically consistent -- a
        # forecaster that nails the temperature should nail the demand.
        actual_temp_c = outdoor_c + rng.gauss(0.0, 1.4)
        actual_irr = max(0.0, irradiance * rng.uniform(0.70, 1.25))
        actual_demand = area * u_value_kw_m2_k * max(
            0.0, (SETPOINT_C - actual_temp_c) - 0.0055 * actual_irr
        )
        actual.append(
            HourlyConditions(
                hour=hour,
                heat_demand_kw=max(0.0, actual_demand),
                co2_demand_kg_h=base.co2_demand_kg_h,
                irradiance_w_m2=actual_irr,
                outdoor_temp_c=round(actual_temp_c, 3),
                power_price_eur_kwh=base.power_price_eur_kwh,
                gas_price_eur_kwh=base.gas_price_eur_kwh,
            )
        )

    return SyntheticDay(
        date=date, seed=seed, forecast=tuple(forecast), actual=tuple(actual)
    )


@dataclass(frozen=True)
class SyntheticHistory:
    """A run of consecutive days, for training and backtesting a forecaster."""

    days: tuple[SyntheticDay, ...]
    seed: int
    start_date: str

    def __len__(self) -> int:
        return len(self.days)

    def __getitem__(self, i: int) -> SyntheticDay:
        return self.days[i]


def synthetic_history(
    n_days: int,
    seed: int = 0,
    *,
    start_date: str = "2023-01-01",
    floor_area_m2: float = 50_000.0,
    winter: bool = True,
    persistence: float = 0.72,
) -> SyntheticHistory:
    """Generate ``n_days`` of consecutive synthetic days with correlated weather.

    The daily mean temperature follows an AR(1) process, so a cold day is likely to
    be followed by another cold day -- which is the property that makes yesterday's
    demand a useful predictor of today's, and therefore the property a forecaster
    must actually exploit rather than fake.

    Two things are deliberate. The realised series carries genuine unpredictable
    noise (see :func:`synthetic_day`), so a forecaster's skill score is bounded well
    below 1: a series that could be fitted perfectly would make a backtest look
    impressive and tell you nothing. And every day gets its own distinct seed, so
    the run contains no repeated days for a model to memorise.

    Args:
        n_days: Number of consecutive days.
        seed: Master seed. The same seed always yields the same history.
        start_date: ISO date of the first day.
        floor_area_m2: Greenhouse area, used to scale demand.
        winter: Winter or summer conditions for the whole run.
        persistence: AR(1) coefficient on the daily mean temperature, in [0, 1).
            Around 0.7 is typical of Dutch daily means.

    Returns:
        A :class:`SyntheticHistory` of ``n_days`` days in chronological order.
    """
    if n_days < 1:
        raise ValueError(f"n_days must be at least 1, got {n_days}")
    if not 0.0 <= persistence < 1.0:
        raise ValueError(f"persistence must be in [0, 1), got {persistence}")

    from datetime import date as _date
    from datetime import timedelta as _timedelta

    rng = random.Random(seed)
    climate_mean = 4.0 if winter else 19.0
    spread = 4.5 if winter else 4.0
    level = climate_mean + rng.gauss(0.0, spread)

    start = _date.fromisoformat(start_date)
    days = []
    for i in range(n_days):
        days.append(
            synthetic_day(
                (start + _timedelta(days=i)).isoformat(),
                seed=seed * 10_000 + i,
                floor_area_m2=floor_area_m2,
                winter=winter,
                mean_temp_c=round(level, 3),
            )
        )
        # AR(1): today's level pulled towards the climate mean, plus a fresh shock.
        # The shock is scaled so the stationary spread stays at `spread`.
        shock = rng.gauss(0.0, spread * math.sqrt(1.0 - persistence**2))
        level = climate_mean + persistence * (level - climate_mean) + shock

    return SyntheticHistory(days=tuple(days), seed=seed, start_date=start_date)
