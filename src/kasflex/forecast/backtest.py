"""Rolling-origin backtesting.

The only honest way to score a forecaster on a time series: walk forward through
the history, and at each step refit using nothing but the past, predict the next
day, and compare against what actually happened. A random train/test split would
let the model learn from the future and report a number that will never be seen
again in production.

Reported alongside the error is a **skill score** against the naive baseline, since
an MAE in kilowatts means nothing on its own -- it depends entirely on how variable
the series is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from kasflex.forecast.features import MIN_HISTORY_DAYS, DayRecord, day_target
from kasflex.forecast.models import Forecaster, SeasonalNaiveForecaster


@dataclass(frozen=True)
class BacktestResult:
    """Out-of-sample performance over a walk-forward run."""

    model: str
    n_test_days: int
    mae_kw: float
    rmse_kw: float
    baseline_mae_kw: float
    skill: float
    """``1 - mae / baseline_mae``. Zero means no better than yesterday's demand;
    negative means worse. One is unreachable on a series with genuine noise, and a
    value near it means the setup is leaking."""
    mean_demand_kw: float
    per_day_mae: tuple[float, ...] = field(default=(), repr=False)

    @property
    def mae_pct_of_mean(self) -> float:
        return 100.0 * self.mae_kw / self.mean_demand_kw if self.mean_demand_kw else 0.0

    def summary(self) -> str:
        return (
            f"{self.model:<16} MAE {self.mae_kw:8.1f} kW "
            f"({self.mae_pct_of_mean:5.1f}% of mean)   "
            f"RMSE {self.rmse_kw:8.1f} kW   skill vs naive {self.skill:+.3f}   "
            f"n={self.n_test_days}"
        )


def rolling_origin_backtest(
    days: Sequence[DayRecord],
    model: Forecaster,
    min_train_days: int = 14,
    refit_every: int = 1,
) -> BacktestResult:
    """Walk forward through ``days``, refitting on the past and scoring the next day.

    Args:
        days: History in chronological order.
        model: The forecaster to score. Refitted in place as the window grows.
        min_train_days: Days of history required before the first prediction.
            Must exceed :data:`~kasflex.forecast.features.MIN_HISTORY_DAYS`, whose
            first days are consumed by the lag features.
        refit_every: Refit every N days rather than daily. Refitting daily is the
            honest default; larger values model an operator who retrains weekly.

    Returns:
        A :class:`BacktestResult` for ``model``, with the naive baseline scored on
        exactly the same days.

    Raises:
        ValueError: if the history is too short to leave any test days.
    """
    if min_train_days <= MIN_HISTORY_DAYS:
        raise ValueError(
            f"min_train_days must exceed {MIN_HISTORY_DAYS}, the days consumed by "
            f"the lag features; got {min_train_days}"
        )
    start = min_train_days
    if start >= len(days):
        raise ValueError(
            f"history of {len(days)} days is too short to test from day {start}; "
            f"supply at least {start + 1} days"
        )

    baseline = SeasonalNaiveForecaster()
    errors: list[float] = []
    sq_errors: list[float] = []
    base_errors: list[float] = []
    per_day: list[float] = []
    actuals: list[float] = []

    for i in range(start, len(days)):
        if (i - start) % max(1, refit_every) == 0:
            model.fit(days, upto=i)          # strictly the past
        truth = day_target(days, i)
        pred = np.asarray(model.predict(days, i), dtype=float)
        if pred.shape != truth.shape:
            raise ValueError(
                f"{model.name} returned {pred.shape} for day {i}, expected {truth.shape}"
            )
        err = np.abs(pred - truth)
        errors.extend(err.tolist())
        sq_errors.extend(((pred - truth) ** 2).tolist())
        per_day.append(float(err.mean()))
        actuals.extend(truth.tolist())
        base_errors.extend(np.abs(baseline.predict(days, i) - truth).tolist())

    mae = float(np.mean(errors))
    base_mae = float(np.mean(base_errors))
    return BacktestResult(
        model=model.name,
        n_test_days=len(per_day),
        mae_kw=mae,
        rmse_kw=float(np.sqrt(np.mean(sq_errors))),
        baseline_mae_kw=base_mae,
        skill=float(1.0 - mae / base_mae) if base_mae > 0 else 0.0,
        mean_demand_kw=float(np.mean(actuals)),
        per_day_mae=tuple(per_day),
    )
