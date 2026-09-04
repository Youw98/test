"""Planner contracts: the baseline must be safe, the LLM planner must replay offline."""

from __future__ import annotations

import json

import pytest

from kasflex.adapters.greenhouse import SurrogateGreenhouse
from kasflex.checker.rules import SafetyChecker
from kasflex.config import ScenarioConfig
from kasflex.controllers.base import PlanningContext
from kasflex.controllers.llm import LlmPlanner, RecordedTrace, TraceStore
from kasflex.controllers.mpc import MpcNotImplementedError, MpcPlanner
from kasflex.controllers.naive import NaivePlanner
from kasflex.controllers.rule_based import RuleBasedPlanner
from kasflex.data.synthetic import synthetic_day
from kasflex.intent import IntentSchemaError, flat_plan


def _context(hub, conditions, **kwargs):
    return PlanningContext(date="2023-01-15", forecast=conditions, hub=hub, **kwargs)


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("winter", [True, False])
def test_baseline_always_passes_verification(seed, winter):
    """R18's fallback is meaningless if the baseline itself fails verification."""
    import dataclasses

    config = ScenarioConfig.from_yaml("configs/scenario_westland_winter.yaml")
    hub = config.hub
    day = synthetic_day("d", seed=seed, floor_area_m2=hub.floor_area_m2, winter=winter)
    nominal = flat_plan("d", heat_source="boiler", lighting_level=0.4)
    outcome = SurrogateGreenhouse().simulate_day(nominal, day.forecast, hub.floor_area_m2)
    conditions = tuple(
        dataclasses.replace(c, heat_demand_kw=outcome.heat_demand_kw[i],
                            co2_demand_kg_h=outcome.co2_demand_kg_h[i])
        for i, c in enumerate(day.forecast)
    )
    plan = RuleBasedPlanner().plan(_context(hub, conditions))
    verdict = SafetyChecker(hub).verify(plan, conditions, outcome.projection())
    assert verdict.accepted, verdict.feedback()


def test_baseline_respects_the_lamp_curfew(hub, conditions):
    plan = RuleBasedPlanner().plan(_context(hub, conditions))
    for hour in RuleBasedPlanner().lamp_curfew_hours:
        assert plan.intervals[hour].lighting_level == 0.0


def test_naive_planner_produces_violations(hub, conditions):
    """The fixture is only useful if it actually breaks things."""
    plan = NaivePlanner().plan(_context(hub, conditions))
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert not verdict.accepted
    assert len(verdict.hard_violations) > 5


def test_mpc_raises_rather_than_pretending(hub, conditions):
    with pytest.raises(MpcNotImplementedError, match="stage 5"):
        MpcPlanner().plan(_context(hub, conditions))


# --- language-model planner ------------------------------------------------


def _valid_response() -> str:
    return json.dumps(
        {
            "intervals": [
                {"hour": h, "heat_source": "boiler", "lighting_level": 0.5,
                 "battery": "idle", "battery_power_kw": 0, "chp_mode": "off",
                 "co2_source": "none", "reasoning": "steady"}
                for h in range(24)
            ]
        }
    )


def test_llm_planner_records_and_replays(tmp_path, hub, conditions):
    """R12: a recorded run must replay exactly, with no API access."""
    store = TraceStore(tmp_path / "traces.jsonl")
    calls = []

    def fake_call(model, system, prompt):
        calls.append(model)
        return _valid_response()

    recording = LlmPlanner(model="test-model", call_fn=fake_call, traces=store)
    first = recording.plan(_context(hub, conditions))
    assert len(calls) == 1

    # Replay-only: no call_fn at all, so a live call is impossible.
    replaying = LlmPlanner(model="test-model", traces=TraceStore(tmp_path / "traces.jsonl"))
    second = replaying.plan(_context(hub, conditions))
    assert len(calls) == 1, "replay must not reach the model"
    assert first.to_json() == second.to_json()


def test_llm_planner_without_trace_or_call_fn_explains_itself(hub, conditions):
    planner = LlmPlanner(model="absent-model", traces=None, call_fn=None)
    with pytest.raises(RuntimeError, match="no recorded trace"):
        planner.plan(_context(hub, conditions))


def test_llm_planner_tolerates_prose_around_the_json(tmp_path, hub, conditions):
    wrapped = f"Here is my plan.\n```json\n{_valid_response()}\n```\nHope it helps."
    planner = LlmPlanner(
        model="m", call_fn=lambda *a: wrapped, traces=TraceStore(tmp_path / "t.jsonl")
    )
    assert len(planner.plan(_context(hub, conditions)).intervals) == 24


def test_llm_planner_rejects_malformed_output(tmp_path, hub, conditions):
    planner = LlmPlanner(model="m", call_fn=lambda *a: "no plan today",
                         traces=TraceStore(tmp_path / "t.jsonl"))
    with pytest.raises(IntentSchemaError):
        planner.plan(_context(hub, conditions))


def test_llm_planner_rejects_wrong_interval_count(tmp_path, hub, conditions):
    short = json.dumps({"intervals": [{"hour": h} for h in range(12)]})
    planner = LlmPlanner(model="m", call_fn=lambda *a: short,
                         traces=TraceStore(tmp_path / "t.jsonl"))
    with pytest.raises(IntentSchemaError, match="12 intervals"):
        planner.plan(_context(hub, conditions))


def test_prompt_contains_limits_and_the_rejection_to_revise(hub, conditions):
    from kasflex.checker.verdict import Severity, Verdict, Violation

    verdict = Verdict(
        accepted=False,
        violations=(
            Violation("grid.import_limit", "electrical", Severity.HARD, 17,
                      9000.0, 6000.0, "kW", "Too much."),
        ),
    )
    prompt = LlmPlanner(model="m").build_prompt(
        _context(hub, conditions, previous_verdict=verdict, revision=1)
    )
    assert "grid.import_limit" in prompt
    assert "hour 17" in prompt
    assert "6000.00 kW" in prompt
    assert "forecast" in prompt.lower()


def test_trace_key_is_prompt_specific():
    a = TraceStore.key_for("m", "prompt one")
    assert a != TraceStore.key_for("m", "prompt two")
    assert a != TraceStore.key_for("other", "prompt one")


def test_trace_store_appends(tmp_path):
    store = TraceStore(tmp_path / "t.jsonl")
    store.append(RecordedTrace("k1", "m", "p", "r", 0, "d"))
    store.append(RecordedTrace("k2", "m", "p2", "r2", 1, "d"))
    assert len(TraceStore(tmp_path / "t.jsonl")) == 2
