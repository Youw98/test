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

    Returns:
        A :class:`SyntheticDay` whose ``forecast`` is what the planner may see and
        whose ``actual`` is what the run is evaluated against (R4).
    """
    rng = random.Random(seed)
    price_level = rng.uniform(0.06, 0.16)
    gas_price = rng.uniform(0.028, 0.055)
    peak_irradiance = rng.uniform(90.0, 210.0) if winter else rng.uniform(450.0, 780.0)
    heat_scale = rng.uniform(0.030, 0.055) if winter else rng.uniform(0.006, 0.018)
    area = floor_area_m2

    forecast: list[HourlyConditions] = []
    actual: list[HourlyConditions] = []

    for hour in range(24):
        # Daylight window: a raised cosine centred on solar noon.
        day_frac = max(0.0, math.sin(math.pi * (hour - 7.0) / 10.0)) if 7 <= hour <= 17 else 0.0
        irradiance = peak_irradiance * day_frac
        outdoor_swing = math.cos(math.pi * (hour - 14.0) / 12.0)
        heat_demand = area * heat_scale * (0.75 - 0.35 * outdoor_swing) * (1.0 - 0.4 * day_frac)
        co2_demand = area * 0.0012 * day_frac

        base = HourlyConditions(
            hour=hour,
            heat_demand_kw=max(0.0, heat_demand),
            co2_demand_kg_h=max(0.0, co2_demand),
            irradiance_w_m2=irradiance,
            power_price_eur_kwh=round(price_level * _PRICE_SHAPE[hour], 5),
            gas_price_eur_kwh=round(gas_price, 5),
        )
        forecast.append(base)

        if not forecast_error:
            actual.append(base)
            continue
        # Realised conditions drift from the forecast: cloud cover and temperature
        # error dominate, and the day-ahead price is known exactly by gate closure.
        actual.append(
            HourlyConditions(
                hour=hour,
                heat_demand_kw=max(0.0, base.heat_demand_kw * rng.uniform(0.88, 1.14)),
                co2_demand_kg_h=base.co2_demand_kg_h,
                irradiance_w_m2=max(0.0, base.irradiance_w_m2 * rng.uniform(0.70, 1.25)),
                power_price_eur_kwh=base.power_price_eur_kwh,
                gas_price_eur_kwh=base.gas_price_eur_kwh,
            )
        )

    return SyntheticDay(
        date=date, seed=seed, forecast=tuple(forecast), actual=tuple(actual)
    )
