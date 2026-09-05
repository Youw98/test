"""Forecasting: leakage, skill, and the failure modes that flatter a model.

The central risk with any forecaster is that it scores well for the wrong reason.
These tests attack that directly: they mutate the future and assert the model
cannot see it, and they check the fitted coefficients are the physically sensible
ones rather than an artifact of how the data was made.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from kasflex.adapters.greenhouse import SurrogateGreenhouse
from kasflex.data.synthetic import synthetic_history
from kasflex.forecast import (
    MIN_HISTORY_DAYS,
    RidgeForecaster,
    SeasonalNaiveForecaster,
    build_history,
    day_features,
    rolling_origin_backtest,
    training_matrix,
)
from kasflex.forecast.models import NotFittedError


@pytest.fixture(scope="module")
def history():
    weather = synthetic_history(90, seed=5, floor_area_m2=50_000.0)
    return build_history(weather.days, SurrogateGreenhouse(), 50_000.0)


# --- leakage ---------------------------------------------------------------


def test_features_never_read_the_target_days_actuals(history):
    """Rewrite the day being predicted; its features must not move."""
    before = day_features(history, 40)
    tampered = list(history)
    tampered[40] = dataclasses.replace(
        tampered[40],
        actual=tuple(
            dataclasses.replace(c, heat_demand_kw=99_999.0) for c in tampered[40].actual
        ),
    )
    after = day_features(tampered, 40)
    assert np.allclose(before, after), (
        "features changed when the target day's realised demand changed, which "
        "means the model can see the answer it is being asked to predict"
    )


def test_training_matrix_excludes_the_target_day_and_everything_after(history):
    before, _ = training_matrix(history, upto=40)
    tampered = list(history)
    for i in range(40, len(tampered)):
        tampered[i] = dataclasses.replace(
            tampered[i],
            actual=tuple(
                dataclasses.replace(c, heat_demand_kw=99_999.0) for c in tampered[i].actual
            ),
        )
    after, _ = training_matrix(tampered, upto=40)
    assert np.allclose(before, after)


def test_features_require_enough_history(history):
    with pytest.raises(ValueError, match="days of history"):
        day_features(history, MIN_HISTORY_DAYS - 1)


def test_training_matrix_refuses_an_impossible_window(history):
    with pytest.raises(ValueError, match="no trainable days"):
        training_matrix(history, upto=MIN_HISTORY_DAYS)


# --- skill -----------------------------------------------------------------


def test_ridge_beats_the_naive_baseline(history):
    result = rolling_origin_backtest(history, RidgeForecaster(), min_train_days=21)
    assert result.skill > 0.2, f"ridge added little over yesterday: {result.summary()}"
    assert result.n_test_days > 50


def test_skill_is_not_suspiciously_perfect(history):
    """A near-perfect score on a noisy series means leakage, not a good model."""
    result = rolling_origin_backtest(history, RidgeForecaster(), min_train_days=21)
    assert result.skill < 0.95, (
        f"skill of {result.skill:.3f} on a series with genuine noise is not "
        f"credible; suspect leakage"
    )


def test_naive_baseline_scores_zero_skill_against_itself(history):
    result = rolling_origin_backtest(history, SeasonalNaiveForecaster(), min_train_days=21)
    assert abs(result.skill) < 1e-9
    assert result.mae_kw == pytest.approx(result.baseline_mae_kw)


def test_the_model_finds_the_physics(history):
    """Degree-hours should dominate. If it does not, the model found an artifact."""
    model = RidgeForecaster()
    model.fit(history, upto=60)
    top = list(model.coefficients())[:2]
    assert "degree_hours" in top or "outdoor_temp_c" in top, (
        f"the strongest predictors were {top}, not temperature; the model has "
        f"latched onto something other than the heat balance"
    )


def test_predictions_are_never_negative(history):
    model = RidgeForecaster()
    model.fit(history, upto=60)
    assert (model.predict(history, 61) >= 0).all()


def test_predict_before_fit_is_an_error(history):
    with pytest.raises(NotFittedError, match="not been fitted"):
        RidgeForecaster().predict(history, 30)


def test_backtest_needs_a_long_enough_history(history):
    with pytest.raises(ValueError, match="too short"):
        rolling_origin_backtest(history[:20], RidgeForecaster(), min_train_days=25)
    with pytest.raises(ValueError, match="must exceed"):
        rolling_origin_backtest(history, RidgeForecaster(), min_train_days=MIN_HISTORY_DAYS)


def test_fitting_is_deterministic(history):
    a, b = RidgeForecaster(), RidgeForecaster()
    a.fit(history, upto=50)
    b.fit(history, upto=50)
    assert np.allclose(a.predict(history, 51), b.predict(history, 51))


# --- history construction --------------------------------------------------


def test_history_demand_comes_from_the_greenhouse_model():
    """Training on the weather generator's demand would forecast a different system."""
    weather = synthetic_history(20, seed=3, floor_area_m2=50_000.0)
    metered = build_history(weather.days, SurrogateGreenhouse(), 50_000.0)
    raw = np.array([c.heat_demand_kw for c in weather.days[10].actual])
    modelled = np.array([c.heat_demand_kw for c in metered[10].actual])
    assert not np.allclose(raw, modelled), (
        "the metered history equals the weather generator's own demand, so the "
        "forecaster would be trained on a different system than the one it feeds"
    )


def test_history_preserves_dates_and_length():
    weather = synthetic_history(15, seed=1, floor_area_m2=50_000.0)
    metered = build_history(weather.days, SurrogateGreenhouse(), 50_000.0)
    assert len(metered) == 15
    assert [d.date for d in metered] == [d.date for d in weather.days]


def test_weather_history_is_autocorrelated():
    """Without persistence, yesterday tells you nothing and the premise collapses."""
    days = synthetic_history(200, seed=2).days
    means = np.array([np.mean([c.outdoor_temp_c for c in d.actual]) for d in days])
    centred = means - means.mean()
    lag1 = float((centred[:-1] * centred[1:]).mean() / centred.var())
    assert lag1 > 0.4, f"daily temperature autocorrelation is only {lag1:.2f}"


def test_history_is_reproducible():
    assert synthetic_history(10, seed=7) == synthetic_history(10, seed=7)
    assert synthetic_history(10, seed=7) != synthetic_history(10, seed=8)


def test_history_rejects_nonsense_arguments():
    with pytest.raises(ValueError, match="n_days"):
        synthetic_history(0)
    with pytest.raises(ValueError, match="persistence"):
        synthetic_history(5, persistence=1.5)
