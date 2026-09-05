"""The optimising scheduler and the learned planner.

The load-bearing test here is :func:`test_scorer_agrees_with_the_checker`. The
scheduler re-derives the hard constraints rather than importing the checker, which
buys purity and costs a risk: the two could drift apart, and the scheduler would
then confidently produce plans that fail verification. That test is what keeps them
honest, and it is the one to fix first if it ever fails.
"""

from __future__ import annotations

import pytest

from kasflex.adapters.greenhouse import SurrogateGreenhouse
from kasflex.checker.rules import SafetyChecker
from kasflex.config import ScenarioConfig
from kasflex.controllers.base import PlanningContext
from kasflex.controllers.rule_based import RuleBasedPlanner
from kasflex.controllers.scheduler import (
    LearnedPlanner,
    OptimizingScheduler,
    score_plan,
)
from kasflex.data.synthetic import synthetic_history
from kasflex.forecast.history import build_history

CONFIG = "configs/scenario_westland_winter.yaml"


@pytest.fixture(scope="module")
def setup():
    config = ScenarioConfig.from_yaml(CONFIG)
    hub = config.hub
    weather = synthetic_history(50, seed=11, floor_area_m2=hub.floor_area_m2)
    history = build_history(weather.days, SurrogateGreenhouse(), hub.floor_area_m2)
    return hub, history


def _context(hub, history, index):
    return PlanningContext(
        date=history[index].date, forecast=history[index].forecast, hub=hub
    )


# --- the scorer must not drift from the checker ----------------------------


@pytest.mark.parametrize("index", [30, 35, 40, 45])
def test_scorer_agrees_with_the_checker(setup, index):
    """Every plan the optimiser calls feasible must pass the real checker."""
    hub, history = setup
    conditions = history[index].forecast
    plan = LearnedPlanner(history=history[:index]).plan(_context(hub, history, index))

    score = score_plan(plan, hub, conditions)
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert score.feasible == verdict.accepted, (
        f"scheduler says feasible={score.feasible} but the checker says "
        f"accepted={verdict.accepted}; scorer found {score.violations}, checker "
        f"found {[v.constraint for v in verdict.hard_violations]}"
    )


def test_scorer_catches_a_deliberately_broken_plan(setup):
    hub, history = setup
    from kasflex.intent import flat_plan

    bad = flat_plan("d", heat_source="none", lighting_level=1.0,
                    battery="discharge", battery_power_kw=99_000.0)
    score = score_plan(bad, hub, history[30].forecast)
    assert not score.feasible
    assert any(v.startswith("battery.power_limit") for v in score.violations)


def test_scorer_rejects_a_nonsense_margin(setup):
    hub, history = setup
    with pytest.raises(ValueError, match="margin"):
        score_plan(RuleBasedPlanner().plan(_context(hub, history, 30)),
                   hub, history[30].forecast, margin=1.5)


# --- the search ------------------------------------------------------------


def test_optimiser_never_returns_something_worse(setup):
    hub, history = setup
    conditions = history[35].forecast
    seed = RuleBasedPlanner().plan(_context(hub, history, 35))
    improved = OptimizingScheduler().optimise(seed, hub, list(conditions))
    assert (
        score_plan(improved, hub, conditions).cost_eur
        <= score_plan(seed, hub, conditions).cost_eur + 1e-6
    )


def test_optimiser_actually_improves_on_the_seed(setup):
    hub, history = setup
    conditions = history[35].forecast
    seed = RuleBasedPlanner().plan(_context(hub, history, 35))
    scheduler = OptimizingScheduler()
    improved = scheduler.optimise(seed, hub, list(conditions))
    assert score_plan(improved, hub, conditions).cost_eur < score_plan(
        seed, hub, conditions
    ).cost_eur, "the search found nothing; it is not doing any work"
    assert scheduler.evaluations > 100


def test_optimiser_respects_its_evaluation_budget(setup):
    hub, history = setup
    scheduler = OptimizingScheduler(max_evaluations=50)
    seed = RuleBasedPlanner().plan(_context(hub, history, 35))
    scheduler.optimise(seed, hub, list(history[35].forecast))
    assert scheduler.evaluations <= 51


def test_optimiser_hands_back_an_infeasible_seed_untouched(setup):
    """With no valid ground to stand on, the caller's fallback must take over."""
    hub, history = setup
    from kasflex.intent import flat_plan

    bad = flat_plan("d", heat_source="none", lighting_level=0.0)
    scheduler = OptimizingScheduler()
    out = scheduler.optimise(bad, hub, list(history[30].forecast))
    assert out == bad
    assert scheduler.evaluations == 1


def test_margin_keeps_storage_away_from_its_limits(setup):
    """The whole point of the margin: fewer hours pinned against the bounds."""
    hub, history = setup
    conditions = history[35].forecast
    seed = RuleBasedPlanner().plan(_context(hub, history, 35))
    tight = OptimizingScheduler(safety_margin=0.0).optimise(seed, hub, list(conditions))
    roomy = OptimizingScheduler(safety_margin=0.45).optimise(seed, hub, list(conditions))
    edge_tight = score_plan(tight, hub, conditions, margin=0.45).margin_violations
    edge_roomy = score_plan(roomy, hub, conditions, margin=0.45).margin_violations
    assert edge_roomy <= edge_tight


# --- the planner -----------------------------------------------------------


def test_learned_planner_beats_the_rule_based_baseline(setup):
    hub, history = setup
    total_base = total_learned = 0.0
    for index in (30, 35, 40, 45):
        conditions = history[index].forecast
        context = _context(hub, history, index)
        total_base += score_plan(
            RuleBasedPlanner().plan(context), hub, conditions
        ).cost_eur
        total_learned += score_plan(
            LearnedPlanner(history=history[:index]).plan(context), hub, conditions
        ).cost_eur
    assert total_learned < total_base, (
        f"learned planner cost EUR {total_learned:,.0f} against the baseline's "
        f"EUR {total_base:,.0f}; it is not earning its complexity"
    )


def test_learned_planner_is_deterministic(setup):
    hub, history = setup
    context = _context(hub, history, 40)
    a = LearnedPlanner(history=history[:40]).plan(context)
    b = LearnedPlanner(history=history[:40]).plan(context)
    assert a.to_json() == b.to_json()


def test_learned_planner_plans_on_its_own_forecast(setup):
    """It must predict demand, not be handed it. Otherwise it is an oracle."""
    hub, history = setup
    planner = LearnedPlanner(history=history[:40])
    planner.plan(_context(hub, history, 40))
    given = [c.heat_demand_kw for c in history[40].forecast]
    predicted = list(planner.last_forecast_kw)
    assert len(predicted) == 24
    assert predicted != pytest.approx(given, rel=1e-6), (
        "the planner's demand matches the conditions it was handed exactly, so it "
        "is not forecasting anything"
    )


def test_oracle_mode_uses_the_supplied_demand(setup):
    hub, history = setup
    planner = LearnedPlanner(history=history[:40], use_forecast_demand=False)
    planner.plan(_context(hub, history, 40))
    given = [c.heat_demand_kw for c in history[40].forecast]
    assert list(planner.last_forecast_kw) == pytest.approx(given)


def test_planner_reports_what_the_search_did(setup):
    hub, history = setup
    planner = LearnedPlanner(history=history[:40])
    plan = planner.plan(_context(hub, history, 40))
    diagnostics = planner.last_diagnostics
    assert diagnostics["evaluations"] > 0
    assert diagnostics["saving_eur"] >= 0
    assert "forecast" in plan.notes.lower()
    assert plan.planner == "learned"
