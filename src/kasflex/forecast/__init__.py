"""Learned demand forecasting from current and historical data.

The planner needs to know what the greenhouse will ask for tomorrow before it can
schedule anything. In the MVP that number was handed to it by the simulator, which
is convenient and completely unrealistic: a grower has weather forecasts and years
of meter readings, not a physics model of their own greenhouse.

This package closes that gap. It learns hourly heat demand from historical data and
a weather forecast, and it is scored the only way a forecaster can honestly be
scored -- against a naive baseline, on days it has never seen, with every model
trained strictly on data that existed before the day it predicts.
"""

from kasflex.forecast.backtest import BacktestResult, rolling_origin_backtest
from kasflex.forecast.features import (
    FEATURE_NAMES,
    MIN_HISTORY_DAYS,
    DayRecord,
    day_features,
    day_target,
    training_matrix,
)
from kasflex.forecast.history import MeteredDay, build_history
from kasflex.forecast.models import (
    Forecaster,
    NotFittedError,
    RidgeForecaster,
    SeasonalNaiveForecaster,
)

__all__ = [
    "FEATURE_NAMES",
    "MIN_HISTORY_DAYS",
    "BacktestResult",
    "DayRecord",
    "Forecaster",
    "MeteredDay",
    "NotFittedError",
    "RidgeForecaster",
    "SeasonalNaiveForecaster",
    "build_history",
    "day_features",
    "day_target",
    "rolling_origin_backtest",
    "training_matrix",
]
