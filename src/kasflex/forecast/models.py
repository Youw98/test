"""Forecasting models.

Two of them, and the pairing is the point: a model is only interesting relative to
the dumbest thing that could have worked. :class:`SeasonalNaiveForecaster` is that
dumb thing -- yesterday, same hour. :class:`RidgeForecaster` has to beat it to earn
its place, and the backtest reports exactly by how much.

Ridge is solved in closed form with numpy, so the package needs no scikit-learn and
installs into the same dependency-light core as everything else. For a target this
smooth, with a physically meaningful degree-hours feature already supplied, a
linear model is a reasonable place to stop; the interface is small enough that
swapping in gradient boosting later is a contained change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from kasflex.forecast.features import (
    DayRecord,
    day_features,
    day_target,
    training_matrix,
)


class NotFittedError(RuntimeError):
    """Raised when a model is asked to predict before it has been fitted."""


@runtime_checkable
class Forecaster(Protocol):
    """Predicts one day of hourly heat demand."""

    name: str

    def fit(self, days: Sequence[DayRecord], upto: int) -> None:
        """Fit on every usable day strictly before ``upto``."""
        ...

    def predict(self, days: Sequence[DayRecord], index: int) -> np.ndarray:
        """Predict the 24 hourly demands for the day at ``index``."""
        ...


@dataclass
class SeasonalNaiveForecaster:
    """Yesterday, same hour. The baseline every other model is measured against.

    It is a genuinely strong baseline for a persistent series, which is why beating
    it by a wide margin should make you suspicious of leakage rather than pleased.
    """

    name: str = "seasonal-naive"
    lag_days: int = 1

    def fit(self, days: Sequence[DayRecord], upto: int) -> None:
        """No parameters to fit. Present so the model satisfies the protocol."""

    def predict(self, days: Sequence[DayRecord], index: int) -> np.ndarray:
        if index < self.lag_days:
            raise ValueError(
                f"cannot predict day {index} from a lag of {self.lag_days} days"
            )
        return day_target(days, index - self.lag_days)


@dataclass
class RidgeForecaster:
    """Ridge regression on weather, calendar and lagged-demand features.

    Attributes:
        alpha: L2 penalty. Features are standardised first, so one value applies
            sensibly across features with very different units.
        clip_negative: Clamp predictions at zero. Heat demand cannot be negative,
            and a linear model will happily predict that it is.
    """

    name: str = "ridge"
    alpha: float = 1.0
    clip_negative: bool = True
    _weights: np.ndarray | None = field(default=None, repr=False)
    _mean: np.ndarray | None = field(default=None, repr=False)
    _scale: np.ndarray | None = field(default=None, repr=False)
    _intercept: float = field(default=0.0, repr=False)
    n_train_samples: int = 0

    def fit(self, days: Sequence[DayRecord], upto: int) -> None:
        """Fit on days strictly before ``upto``.

        Raises:
            ValueError: if there is nothing trainable before ``upto``.
        """
        x, y = training_matrix(days, upto)
        self._mean = x.mean(axis=0)
        # A constant feature has zero spread; leave it at 1 so it scales to zero
        # rather than producing an infinity.
        scale = x.std(axis=0)
        scale[scale < 1e-12] = 1.0
        self._scale = scale
        xs = (x - self._mean) / scale

        # Centre the target so the intercept is exact and is not penalised.
        self._intercept = float(y.mean())
        yc = y - self._intercept

        n_features = xs.shape[1]
        gram = xs.T @ xs + self.alpha * np.eye(n_features)
        self._weights = np.linalg.solve(gram, xs.T @ yc)
        self.n_train_samples = int(xs.shape[0])

    def predict(self, days: Sequence[DayRecord], index: int) -> np.ndarray:
        if self._weights is None or self._mean is None or self._scale is None:
            raise NotFittedError(
                f"{self.name} has not been fitted; call fit(days, upto) first"
            )
        xs = (day_features(days, index) - self._mean) / self._scale
        pred = xs @ self._weights + self._intercept
        return np.maximum(pred, 0.0) if self.clip_negative else pred

    def coefficients(self) -> dict[str, float]:
        """Standardised coefficients, largest magnitude first.

        Useful as a sanity check: if ``degree_hours`` is not near the top, the model
        has found something other than physics and is worth distrusting.
        """
        if self._weights is None:
            raise NotFittedError(f"{self.name} has not been fitted")
        from kasflex.forecast.features import FEATURE_NAMES

        pairs = dict(zip(FEATURE_NAMES, (float(w) for w in self._weights), strict=True))
        return dict(sorted(pairs.items(), key=lambda kv: -abs(kv[1])))
