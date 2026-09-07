"""Features for hourly heat-demand forecasting.

Two sources, kept strictly apart, because mixing them is the classic way to build a
forecaster that scores brilliantly and is worthless:

* **Weather for the target day** comes from the *forecast* series. At planning time
  that is genuinely all you have.
* **Lagged demand** comes from the *actual* series of days strictly before the
  target day. Yesterday's meter reading is known; today's is not.

Nothing in this module ever reads the target day's actuals. :func:`day_features`
takes the day index and slices the history itself, so a caller cannot pass the
answer in by mistake.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from kasflex.energy.dispatch import HourlyConditions

HOURS = 24
LAG_DAYS: tuple[int, ...] = (1, 2, 7)
MIN_HISTORY_DAYS = max(LAG_DAYS)
"""Days of history needed before any features can be built at all."""

BALANCE_POINT_C = 18.5
"""Temperature below which a greenhouse needs heat. Degree-hours below this point
are the physically meaningful predictor, and giving the model that term directly
means it does not have to discover it from data it may not have enough of."""

FEATURE_NAMES: tuple[str, ...] = (
    "degree_hours",
    "outdoor_temp_c",
    "irradiance_w_m2",
    "solar_offset",
    "sin_hour",
    "cos_hour",
    "sin_2hour",
    "cos_2hour",
    "lag1_same_hour",
    "lag2_same_hour",
    "lag7_same_hour",
    "mean7_same_hour",
    "mean_last_day",
    "lag1_daily_mean_temp",
)


@runtime_checkable
class DayRecord(Protocol):
    """A day of forecast and realised conditions.

    Structural, not nominal: :class:`kasflex.data.synthetic.SyntheticDay` satisfies
    it, and so will a record built from cached ENTSO-E and KNMI series.
    """

    date: str
    forecast: tuple[HourlyConditions, ...]
    actual: tuple[HourlyConditions, ...]


@dataclass(frozen=True)
class PlainDay:
    """A minimal :class:`DayRecord`, for building histories from real data."""

    date: str
    forecast: tuple[HourlyConditions, ...]
    actual: tuple[HourlyConditions, ...]


def _demand(day: DayRecord) -> np.ndarray:
    return np.array([c.heat_demand_kw for c in day.actual], dtype=float)


def day_features(days: Sequence[DayRecord], index: int) -> np.ndarray:
    """Build the feature matrix for the day at ``index``.

    Args:
        days: History in chronological order.
        index: Position of the day to build features for. Must be at least
            :data:`MIN_HISTORY_DAYS`, since the lags reach that far back.

    Returns:
        An array of shape ``(24, len(FEATURE_NAMES))``.

    Raises:
        IndexError: if ``index`` is out of range.
        ValueError: if there is not enough history behind ``index``.
    """
    if not 0 <= index < len(days):
        raise IndexError(f"index {index} out of range for a history of {len(days)} days")
    if index < MIN_HISTORY_DAYS:
        raise ValueError(
            f"day {index} has only {index} days of history behind it; "
            f"{MIN_HISTORY_DAYS} are needed for the lag features"
        )

    target = days[index]
    lags = {d: _demand(days[index - d]) for d in LAG_DAYS}
    last7 = np.stack([_demand(days[index - d]) for d in range(1, 8)])
    mean7_by_hour = last7.mean(axis=0)
    mean_last_day = float(lags[1].mean())
    lag1_mean_temp = float(np.mean([c.outdoor_temp_c for c in days[index - 1].actual]))

    rows = []
    for hour in range(HOURS):
        weather = target.forecast[hour]
        degree_hours = max(0.0, BALANCE_POINT_C - weather.outdoor_temp_c)
        rows.append(
            [
                degree_hours,
                weather.outdoor_temp_c,
                weather.irradiance_w_m2,
                # Sunlight offsets part of the heating need; the product is what
                # actually reduces demand, so give the model the interaction.
                degree_hours * weather.irradiance_w_m2 / 1000.0,
                math.sin(2 * math.pi * hour / HOURS),
                math.cos(2 * math.pi * hour / HOURS),
                math.sin(4 * math.pi * hour / HOURS),
                math.cos(4 * math.pi * hour / HOURS),
                lags[1][hour],
                lags[2][hour],
                lags[7][hour],
                mean7_by_hour[hour],
                mean_last_day,
                lag1_mean_temp,
            ]
        )
    return np.asarray(rows, dtype=float)


def day_target(days: Sequence[DayRecord], index: int) -> np.ndarray:
    """Realised hourly heat demand for the day at ``index``. Never a feature."""
    return _demand(days[index])


def training_matrix(
    days: Sequence[DayRecord], upto: int
) -> tuple[np.ndarray, np.ndarray]:
    """Stack features and targets for every usable day strictly before ``upto``.

    ``upto`` is exclusive. This is the whole leakage guarantee: a model fitted on
    ``training_matrix(days, i)`` has seen nothing from day ``i`` or later.

    Returns:
        ``(X, y)`` with shapes ``(n * 24, n_features)`` and ``(n * 24,)``.

    Raises:
        ValueError: if no day before ``upto`` has enough history behind it.
    """
    usable = range(MIN_HISTORY_DAYS, min(upto, len(days)))
    if not usable:
        raise ValueError(
            f"no trainable days before index {upto}: the first {MIN_HISTORY_DAYS} "
            f"days of any history are consumed by the lag features"
        )
    xs = [day_features(days, i) for i in usable]
    ys = [day_target(days, i) for i in usable]
    return np.vstack(xs), np.concatenate(ys)
