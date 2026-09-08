"""Run one scenario end to end and produce one structured result record (R33).

The order of operations is the experiment design, so it is worth stating plainly:

1. The planner sees the **forecast** and produces a plan.
2. The checker verifies the plan against the **forecast**. On rejection the planner
   may revise, up to ``max_revisions``, after which control passes to the
   rule-based baseline (R18).
3. A human approves, edits or rejects. An edit is re-verified before acceptance (R23).
4. The accepted plan is executed against the **actuals**.
5. The realised day is audited by a checker with every check enabled, regardless of
   the experimental condition. That audit is the measurement: it is how violations
   can be counted with the checker disabled and shown to be zero with it enabled
   (acceptance criterion 3).

Step 5 is why the checker configuration under test never touches the audit. If the
same excluded check were skipped in both places, an unverified run would score as
clean and the headline table would be meaningless.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from kasflex.adapters.greenhouse import DayOutcome, GreenhouseModel, SurrogateGreenhouse
from kasflex.checker.rules import CheckerConfig, SafetyChecker
from kasflex.checker.verdict import Severity, Verdict
from kasflex.controllers.base import Planner, PlanningContext
from kasflex.controllers.rule_based import RuleBasedPlanner
from kasflex.energy.assets import EnergyHub
from kasflex.energy.dispatch import DispatchResult, HourlyConditions, dispatch_plan
from kasflex.intent import IntentSchemaError, Plan
from kasflex.oversight import Approver, AuditLog, AutoApprover, Decision


@dataclass(frozen=True)
class RunResult:
    """One row of the experiment matrix (R33)."""

    scenario: str
    date: str
    planner: str
    greenhouse_model: str
    checker_enabled: bool
    checker_explains: bool
    checks_excluded: tuple[str, ...]
    seed: int
    revisions_used: int
    fell_back_to_baseline: bool
    human_decision: str
    accepted: bool
    plan: Plan
    verdict: Verdict
    dispatch: DispatchResult
    outcome: DayOutcome
    realised_violations: tuple[dict[str, Any], ...]
    """Violations found by auditing the realised day with ALL checks enabled."""
    metrics: dict[str, float] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def realised_hard_violations(self) -> int:
        return sum(1 for v in self.realised_violations if v["severity"] == Severity.HARD.value)

    def to_record(self) -> dict[str, Any]:
        """Flat, JSON-serialisable record containing all conditions and outcomes."""
        by_category: dict[str, int] = {}
        for v in self.realised_violations:
            by_category[v["category"]] = by_category.get(v["category"], 0) + 1
        return {
            "scenario": self.scenario,
            "date": self.date,
            "planner": self.planner,
            "greenhouse_model": self.greenhouse_model,
            "checker_enabled": self.checker_enabled,
            "checker_explains": self.checker_explains,
            "checks_excluded": list(self.checks_excluded),
            "seed": self.seed,
            "revisions_used": self.revisions_used,
            "fell_back_to_baseline": self.fell_back_to_baseline,
            "human_decision": self.human_decision,
            "accepted": self.accepted,
            "plan": self.plan.to_dict(),
            "verdict": self.verdict.to_dict(),
            "realised_violations_total": len(self.realised_violations),
            "realised_violations_hard": self.realised_hard_violations,
            "realised_violations_by_category": by_category,
            "realised_violations": list(self.realised_violations),
            "hub": dataclasses.asdict(self.dispatch.hub),
            **self.metrics,
            "provenance": self.provenance,
        }


def _conditions_with_demand(
    base: tuple[HourlyConditions, ...], outcome: DayOutcome
) -> tuple[HourlyConditions, ...]:
    """Attach the greenhouse's heat and CO2 demand to a set of conditions."""
    return tuple(
        dataclasses.replace(
            c,
            heat_demand_kw=outcome.heat_demand_kw[i],
            co2_demand_kg_h=outcome.co2_demand_kg_h[i],
        )
        for i, c in enumerate(base)
    )


def _conditions_digest(series: tuple[HourlyConditions, ...]) -> str:
    """Stable content hash for the exact series used by a recorded run."""
    payload = json.dumps(
        [dataclasses.asdict(row) for row in series],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_scenario(
    *,
    scenario: str,
    date: str,
    hub: EnergyHub,
    forecast: tuple[HourlyConditions, ...],
    actual: tuple[HourlyConditions, ...],
    planner: Planner | None = None,
    greenhouse: GreenhouseModel | None = None,
    checker_config: CheckerConfig | None = None,
    approver: Approver | None = None,
    audit_log: AuditLog | None = None,
    brief: str = "",
    seed: int = 0,
    provenance: dict[str, Any] | None = None,
) -> RunResult:
    """Plan, verify, approve, execute and score one day.

    Args:
        scenario: Scenario name, carried into the result record.
        date: ISO date of the simulated day.
        hub: Energy hub configuration, including all limits.
        forecast: What the planner is allowed to see.
        actual: What actually happens. Never shown to the planner (R4).
        planner: Defaults to the rule-based baseline.
        greenhouse: Defaults to the unvalidated surrogate.
        checker_config: The experimental condition under test.
        approver: Defaults to automatic approval.
        audit_log: Append-only record of the run (R25).
        brief: Plain-language operator brief (R24).
        seed: Carried into the record for reproducibility (R6).

    Returns:
        A :class:`RunResult`.
    """
    planner = planner or RuleBasedPlanner()
    greenhouse = greenhouse or SurrogateGreenhouse()
    checker_config = checker_config or CheckerConfig()
    approver = approver or AutoApprover()
    baseline = RuleBasedPlanner()
    checker = SafetyChecker(hub=hub, config=checker_config)

    def log(kind: str, payload: dict[str, Any]) -> None:
        if audit_log is not None:
            audit_log.append(kind, payload, operator=getattr(approver, "name", ""))

    # A nominal plan gives the greenhouse model something to run so that the
    # forecast carries a heat and CO2 demand for the planner to reason about.
    # It is the baseline's own plan, so the demand profile is not tuned to any
    # planner under test.
    seed_context = PlanningContext(date=date, forecast=forecast, hub=hub, brief=brief)
    nominal_plan = baseline.plan(seed_context)
    forecast_outcome = greenhouse.simulate_day(nominal_plan, forecast, hub.floor_area_m2)
    forecast_conditions = _conditions_with_demand(forecast, forecast_outcome)

    log(
        "run_started",
        {
            "scenario": scenario,
            "date": date,
            "planner": getattr(planner, "name", "?"),
            "checker_enabled": checker_config.enabled,
            "seed": seed,
        },
    )

    # --- plan / verify / revise loop (R18) --------------------------------
    plan: Plan | None = None
    verdict: Verdict | None = None
    plan_conditions: tuple[HourlyConditions, ...] | None = None
    plan_outcome: DayOutcome | None = None
    revisions_used = 0
    fell_back = False

    for attempt in range(checker_config.max_revisions + 1):
        context = PlanningContext(
            date=date,
            forecast=forecast_conditions,
            hub=hub,
            brief=brief,
            previous_verdict=verdict,
            previous_plan=plan,
            revision=attempt,
        )
        try:
            candidate = planner.plan(context)
        except IntentSchemaError as exc:
            # A malformed plan is treated exactly like a rejection: the planner gets
            # told what was wrong and may try again. Crashing here would make a
            # flaky model look like a broken experiment.
            log("plan_malformed", {"attempt": attempt, "error": str(exc)})
            verdict = Verdict(
                accepted=False,
                violations=(),
                enabled=checker_config.enabled,
                plan_revision=attempt,
                metadata={"schema_error": str(exc)},
            )
            revisions_used = attempt
            continue

        # Crop projections must follow the candidate being judged. Reusing the
        # nominal baseline projection here can make a plan that removes all CO2 or
        # light appear safe simply because the baseline supplied it.
        candidate_outcome = greenhouse.simulate_day(candidate, forecast, hub.floor_area_m2)
        candidate_conditions = _conditions_with_demand(forecast, candidate_outcome)
        candidate_verdict = checker.verify(
            candidate, candidate_conditions, candidate_outcome.projection()
        )
        log("plan_proposed", {"attempt": attempt, "plan": candidate.to_dict()})
        log("verdict", {"attempt": attempt, "verdict": candidate_verdict.to_dict()})

        plan, verdict = candidate, candidate_verdict
        plan_conditions, plan_outcome = candidate_conditions, candidate_outcome
        revisions_used = attempt
        if candidate_verdict.accepted:
            break
    else:
        # Every attempt was rejected: hand control to the baseline (R18).
        fell_back = True
        plan = baseline.plan(
            PlanningContext(date=date, forecast=forecast_conditions, hub=hub, brief=brief)
        )
        plan_outcome = greenhouse.simulate_day(plan, forecast, hub.floor_area_m2)
        plan_conditions = _conditions_with_demand(forecast, plan_outcome)
        verdict = checker.verify(plan, plan_conditions, plan_outcome.projection())
        log(
            "fallback_to_baseline",
            {"after_revisions": revisions_used, "accepted": verdict.accepted},
        )

    assert plan is not None and verdict is not None
    assert plan_conditions is not None and plan_outcome is not None

    # --- human oversight (R22, R23) ---------------------------------------
    decision = approver.review(plan, verdict)
    log(
        "human_decision",
        {
            "decision": decision.decision.value,
            "comment": decision.comment,
            "seconds_to_decide": decision.seconds_to_decide,
        },
    )
    if decision.decision is Decision.EDIT:
        plan = decision.plan
        plan_outcome = greenhouse.simulate_day(plan, forecast, hub.floor_area_m2)
        plan_conditions = _conditions_with_demand(forecast, plan_outcome)
        verdict = checker.verify(plan, plan_conditions, plan_outcome.projection())
        log("reverified_after_edit", {"verdict": verdict.to_dict()})

    # A human edit is not a waiver. If verification is enabled, a rejected edit
    # must never reach execution; falling back is safer and keeps the run complete.
    accepted = decision.decision is not Decision.REJECT and (
        not checker_config.enabled or verdict.accepted
    )
    if not accepted:
        # A rejected plan is never executed. The day falls back to the baseline,
        # which is what an operator would actually do.
        plan = baseline.plan(
            PlanningContext(date=date, forecast=forecast_conditions, hub=hub, brief=brief)
        )
        plan_outcome = greenhouse.simulate_day(plan, forecast, hub.floor_area_m2)
        plan_conditions = _conditions_with_demand(forecast, plan_outcome)
        verdict = checker.verify(plan, plan_conditions, plan_outcome.projection())
        fell_back = True
        log(
            "human_rejected_using_baseline",
            {
                "reason": "operator rejection"
                if decision.decision is Decision.REJECT
                else "edited plan failed re-verification",
                "fallback_verdict": verdict.to_dict(),
            },
        )

    # --- execute against the actuals --------------------------------------
    realised_outcome = greenhouse.simulate_day(plan, actual, hub.floor_area_m2)
    realised_conditions = _conditions_with_demand(actual, realised_outcome)
    realised_dispatch = dispatch_plan(plan, hub, list(realised_conditions))

    # --- audit the realised day with EVERY check enabled ------------------
    # Deliberately independent of the condition under test. This is the measurement.
    auditor = SafetyChecker(hub=hub, config=CheckerConfig(enabled=True, excluded_checks=()))
    audit = auditor.verify(plan, realised_conditions, realised_outcome.projection())
    log("realised_audit", {"violations": len(audit.violations)})

    summary = realised_dispatch.summary()
    metrics = {
        **summary,
        "fruit_growth_kg_m2": round(realised_outcome.fruit_growth_kg_m2, 6),
        "natural_dli_mol_m2": round(realised_outcome.natural_dli_mol_m2, 3),
        "supplemental_dli_mol_m2": round(realised_dispatch.total_dli_mol_m2, 3),
        "temperature_band_hours": float(
            realised_outcome.temperature_band_hours(hub.crop.temp_min_c, hub.crop.temp_max_c)
        ),
        "heat_dumped_kwh": round(sum(iv.heat_dumped_kw for iv in realised_dispatch.intervals), 2),
    }

    result = RunResult(
        scenario=scenario,
        date=date,
        planner=getattr(planner, "name", "unknown"),
        greenhouse_model=realised_outcome.model,
        checker_enabled=checker_config.enabled,
        checker_explains=checker_config.explain,
        checks_excluded=tuple(checker_config.excluded_checks),
        seed=seed,
        revisions_used=revisions_used,
        fell_back_to_baseline=fell_back,
        human_decision=decision.decision.value,
        accepted=accepted,
        plan=plan,
        verdict=verdict,
        dispatch=realised_dispatch,
        outcome=realised_outcome,
        realised_violations=tuple(v.to_dict() for v in audit.violations),
        metrics=metrics,
        provenance={
            "greenhouse_validated": realised_outcome.validated,
            "data_source": (provenance or {}).get("data_source", "synthetic"),
            "forecast_sha256": _conditions_digest(forecast),
            "actual_sha256": _conditions_digest(actual),
            **(provenance or {}),
        },
    )
    log(
        "run_finished", {"metrics": metrics, "realised_violations": len(result.realised_violations)}
    )
    return result
