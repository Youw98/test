"""The measurement the whole system exists to produce.

Acceptance criterion 3: violations are measurable with the checker disabled and
zero with it enabled. Acceptance criterion 5: at least one case where the AI
performs worse than the baseline is identified.
"""

from __future__ import annotations

import json
from pathlib import Path

from kasflex.adapters.greenhouse import SurrogateGreenhouse
from kasflex.checker.rules import CheckerConfig
from kasflex.config import ScenarioConfig
from kasflex.controllers.naive import NaivePlanner
from kasflex.controllers.rule_based import RuleBasedPlanner
from kasflex.data.synthetic import synthetic_day
from kasflex.energy.assets import EnergyHub
from kasflex.experiment import ExperimentMatrix, summarise
from kasflex.intent import flat_plan
from kasflex.oversight import AuditLog, CallbackApprover, Decision, HumanDecision
from kasflex.run import run_scenario


def _run(planner, enabled, seed=5, audit=None):
    day = synthetic_day("2023-01-15", seed=seed)
    return run_scenario(
        scenario="e2e",
        date="2023-01-15",
        hub=EnergyHub(),
        forecast=day.forecast,
        actual=day.actual,
        planner=planner,
        checker_config=CheckerConfig(enabled=enabled),
        audit_log=audit,
        seed=seed,
    )


def test_checker_disabled_produces_violations_enabled_produces_none():
    unverified = _run(NaivePlanner(), enabled=False)
    verified = _run(NaivePlanner(), enabled=True)
    assert unverified.realised_hard_violations > 0, (
        "an unverified constraint-blind planner must produce measurable violations, "
        "or the headline comparison has nothing to compare"
    )
    assert verified.realised_hard_violations == 0
    assert verified.fell_back_to_baseline


def test_baseline_never_violates():
    result = _run(RuleBasedPlanner(), enabled=True)
    assert result.realised_hard_violations == 0
    assert not result.fell_back_to_baseline


def test_unverified_planner_can_be_worse_than_the_baseline():
    """Acceptance criterion 5, in its cheapest form."""
    baseline = _run(RuleBasedPlanner(), enabled=True)
    unverified = _run(NaivePlanner(), enabled=False)
    assert unverified.metrics["net_cost_eur"] > baseline.metrics["net_cost_eur"]


def test_revision_loop_falls_back_after_max_revisions():
    day = synthetic_day("2023-01-15", seed=5)
    result = run_scenario(
        scenario="fallback",
        date="2023-01-15",
        hub=EnergyHub(),
        forecast=day.forecast,
        actual=day.actual,
        planner=NaivePlanner(),
        checker_config=CheckerConfig(enabled=True, max_revisions=2),
        seed=5,
    )
    assert result.fell_back_to_baseline
    assert result.revisions_used == 2


def test_unsafe_human_edit_is_reverified_and_never_executed(tmp_path):
    """A human edit is not a waiver of an enabled safety check."""
    day = synthetic_day("2023-01-15", seed=5)
    hub = EnergyHub()
    unsafe_edit = flat_plan(
        "2023-01-15",
        planner="operator-edit",
        heat_source="boiler",
        lighting_level=0.63,
        battery="discharge",
        battery_power_kw=hub.battery.max_discharge_kw,
        co2_source="liquid",
    )
    actual_executions = []
    surrogate = SurrogateGreenhouse()

    class RecordingGreenhouse:
        name = "recording-surrogate"

        def simulate_day(self, plan, conditions, floor_area_m2):
            if conditions is day.actual:
                actual_executions.append(plan)
            return surrogate.simulate_day(plan, conditions, floor_area_m2)

    log = AuditLog(tmp_path / "audit.jsonl")
    result = run_scenario(
        scenario="unsafe-edit",
        date="2023-01-15",
        hub=hub,
        forecast=day.forecast,
        actual=day.actual,
        planner=RuleBasedPlanner(),
        greenhouse=RecordingGreenhouse(),
        checker_config=CheckerConfig(enabled=True),
        approver=CallbackApprover(
            lambda _plan, _verdict: HumanDecision(
                decision=Decision.EDIT,
                plan=unsafe_edit,
                comment="unsafe test edit",
            )
        ),
        audit_log=log,
        seed=5,
    )

    entries = log.entries()
    reverified = next(entry for entry in entries if entry["kind"] == "reverified_after_edit")
    assert reverified["payload"]["verdict"]["accepted"] is False
    fallback = next(entry for entry in entries if entry["kind"] == "human_rejected_using_baseline")

    assert result.human_decision == Decision.EDIT.value
    assert result.accepted is False
    assert result.fell_back_to_baseline
    assert result.verdict.accepted is True
    assert result.plan.planner == "rule-based"
    assert result.plan != unsafe_edit
    assert fallback["payload"]["fallback_verdict"] == result.verdict.to_dict()
    assert actual_executions == [result.plan]
    assert unsafe_edit not in actual_executions


def test_audit_uses_all_checks_even_when_one_is_excluded(tmp_path):
    """The audit must ignore the condition under test, or an unverified run scores clean."""
    day = synthetic_day("2023-01-15", seed=5)
    result = run_scenario(
        scenario="gap",
        date="2023-01-15",
        hub=EnergyHub(),
        forecast=day.forecast,
        actual=day.actual,
        planner=NaivePlanner(),
        checker_config=CheckerConfig(
            enabled=True, max_revisions=0, excluded_checks=("grid.import_limit",)
        ),
        seed=5,
    )
    assert "grid.import_limit" in result.checks_excluded
    audited = {v["constraint"] for v in result.realised_violations}
    if result.accepted and not result.fell_back_to_baseline:
        assert "grid.import_limit" in audited, (
            "the excluded check must still be applied when auditing the realised day"
        )


def test_audit_log_is_append_only_and_records_the_whole_run(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    _run(NaivePlanner(), enabled=True, audit=log)
    entries = log.entries()
    kinds = [e["kind"] for e in entries]
    for expected in (
        "run_started",
        "plan_proposed",
        "verdict",
        "human_decision",
        "realised_audit",
        "run_finished",
    ):
        assert expected in kinds, f"{expected} missing from the audit log"

    before = len(entries)
    _run(RuleBasedPlanner(), enabled=True, audit=log)
    assert len(log.entries()) > before, "the log must append, never overwrite"
    assert log.entries()[:before] == entries, "existing entries must not be rewritten"


def test_anonymous_mode_hides_the_operator(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", anonymous=True)
    log.append("test", {}, operator="grower-42")
    assert log.entries()[0]["operator"] == "anonymous"


def test_experiment_matrix_runs_unattended_and_writes_records(tmp_path):
    """R32 and R33."""
    config = ScenarioConfig.from_yaml("configs/scenario_westland_winter.yaml")
    out = tmp_path / "runs.jsonl"
    matrix = ExperimentMatrix(
        config=ScenarioConfig(**{**config.__dict__, "audit_path": str(tmp_path / "audit.jsonl")}),
        days=1,
        output_path=str(out),
    )
    records = matrix.run(verbose=False)
    assert len(records) == len(matrix.conditions)
    written = [json.loads(line) for line in Path(out).read_text().splitlines() if line.strip()]
    assert len(written) == len(records)

    summary = summarise(records)
    assert summary["ai-unverified"]["mean_violations"] > 0
    assert summary["ai-verified"]["mean_violations"] == 0
    assert summary["rule-based"]["mean_violations"] == 0
    # The MPC arm is not implemented yet; it must be recorded as an error, not
    # silently omitted, so the gap in the matrix stays visible.
    assert any(r.get("status") == "error" and r["condition"] == "mpc" for r in records)


def test_runs_offline_with_no_network(monkeypatch):
    """Acceptance criterion 2: scenarios run on a laptop with no network access."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the run attempted a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    result = _run(RuleBasedPlanner(), enabled=True)
    assert result.metrics["net_cost_eur"] > 0
