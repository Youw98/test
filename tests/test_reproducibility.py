"""R6: identical seed and configuration must produce identical results.

This is not a nicety. Acceptance criterion 7 asks an external researcher to
regenerate every published figure with one command; if a run is not bit-stable, no
figure can be regenerated and no result can be checked.
"""

from __future__ import annotations

import json

from kasflex.checker.rules import CheckerConfig
from kasflex.controllers.naive import NaivePlanner
from kasflex.data.synthetic import synthetic_day
from kasflex.energy.assets import EnergyHub
from kasflex.run import run_scenario


def _run(seed: int, enabled: bool = True):
    day = synthetic_day("2023-01-15", seed=seed)
    return run_scenario(
        scenario="repro",
        date="2023-01-15",
        hub=EnergyHub(),
        forecast=day.forecast,
        actual=day.actual,
        planner=NaivePlanner(),
        checker_config=CheckerConfig(enabled=enabled),
        seed=seed,
    )


def test_synthetic_day_is_deterministic():
    assert synthetic_day("d", seed=11) == synthetic_day("d", seed=11)
    assert synthetic_day("d", seed=11) != synthetic_day("d", seed=12)


def test_identical_seed_gives_identical_record():
    a, b = _run(3), _run(3)
    assert json.dumps(a.to_record(), sort_keys=True, default=str) == json.dumps(
        b.to_record(), sort_keys=True, default=str
    )


def test_different_seed_gives_different_conditions():
    assert _run(3).metrics["net_cost_eur"] != _run(4).metrics["net_cost_eur"]


def test_plans_are_stable_across_runs():
    assert _run(7).plan.to_json() == _run(7).plan.to_json()


def test_forecast_and_actual_are_kept_separate():
    """R4: the planner must never be handed the realised conditions."""
    day = synthetic_day("d", seed=5)
    assert day.forecast != day.actual, (
        "with forecast_error enabled the two series must differ, or the experiment "
        "cannot distinguish planner error from forecast error"
    )
    identical = synthetic_day("d", seed=5, forecast_error=False)
    assert identical.forecast == identical.actual
