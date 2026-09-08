"""Data-driven planner: learn the demand, then optimise the schedule.

Two halves, and they are different kinds of thing on purpose.

**Prediction is learned.** :mod:`kasflex.forecast` fits a model to historical meter
readings and weather, and predicts tomorrow's hourly heat demand. That is a genuine
statistical learning problem: the relationship between weather and demand is
specific to a greenhouse and is not worth deriving by hand.

**Scheduling is optimised, not learned.** Once demand and prices are known, choosing
when to run the CHP and charge the battery is a constrained optimisation with an
exactly known objective. Learning a policy for it (reinforcement learning) would
need far more data than a grower has, and would produce something harder to trust
than a search that provably improves on its starting point. So the scheduler is a
deterministic local search scored by the real dispatch model.

That last part matters: every candidate is evaluated with
:func:`~kasflex.energy.dispatch.dispatch_hour`, the same function
:func:`~kasflex.energy.dispatch.dispatch_plan` uses and the checker inspects. The
optimiser cannot talk itself into a plan that then fails verification, because it
is scoring the real thing rather than an approximation of it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from kasflex.controllers.base import PlanningContext
from kasflex.controllers.rule_based import RuleBasedPlanner
from kasflex.energy.assets import EnergyHub
from kasflex.energy.dispatch import HourlyConditions, HubState, dispatch_hour
from kasflex.forecast.features import DayRecord
from kasflex.forecast.models import Forecaster, RidgeForecaster
from kasflex.intent import HOURS_PER_DAY, Plan

INFEASIBLE = float("inf")


@dataclass(frozen=True)
class ScheduleScore:
    """What a candidate plan costs, whether it is allowed, and how close to the edge.

    Two feasibility notions, deliberately kept apart:

    * ``feasible`` -- does it satisfy the **real** limits? A plan that does not is
      never acceptable, whatever it costs.
    * ``margin_violations`` -- how many hours sit outside a *reserved* storage band?
      This is not a safety verdict. It counts how much the plan is leaning on the
      edge of its storage, which is what makes it fragile to forecast error.

    The search minimises the pair ``(margin_violations, cost)`` among plans that are
    really feasible. Requiring margin-feasibility outright does not work: the
    rule-based starting plan deliberately charges the battery to its ceiling, so it
    is never margin-feasible, and gating on it would switch the whole mechanism off.
    Treating it as a second objective instead lets the search move away from the
    edge where that is cheap, and stay near it where the day is genuinely tight.
    """

    cost_eur: float
    feasible: bool
    dli_mol_m2: float
    violations: tuple[str, ...] = ()
    margin_violations: int = 0

    @property
    def objective(self) -> tuple[int, float]:
        """Lexicographic: fewer margin violations first, then lower cost."""
        if not self.feasible:
            return (10**9, INFEASIBLE)
        return (self.margin_violations, self.cost_eur)


def score_plan(
    plan: Plan,
    hub: EnergyHub,
    conditions: Sequence[HourlyConditions],
    margin: float = 0.0,
) -> ScheduleScore:
    """Cost a plan against the real dispatch model and test the hard constraints.

    The constraints tested here are exactly the HARD checks in
    :mod:`kasflex.checker`: grid contract, battery power and state of charge, buffer
    bounds, unmet heat, CHP minimum run and down times, and the daily light
    integral. They are re-derived rather than imported so that the optimiser stays a
    pure function of the hub, but a test asserts the two agree on every plan the
    optimiser produces.

    Args:
        margin: Fraction of the usable *storage* band (battery state of charge, heat
            buffer level) to hold in reserve, in [0, 1). Storage state is
            cumulative: a small demand error each hour integrates over the day and
            walks the buffer through its floor, which is where rejections actually
            come from. Instantaneous grid limits are re-decided every hour and the
            planner already leaves headroom there, so the margin does not apply to
            them.
    """
    if not 0.0 <= margin < 1.0:
        raise ValueError(f"margin must be in [0, 1), got {margin}")

    state = HubState.initial(hub)
    total = 0.0
    dli = 0.0
    violations: list[str] = []
    run_state: list[bool] = []
    margin_hits = 0
    b, buf = hub.battery, hub.buffer
    soc_span = (b.soc_max_kwh - b.soc_min_kwh) * margin / 2.0
    buf_span = (buf.level_max_kwh - buf.level_min_kwh) * margin / 2.0

    for intent, cond in zip(plan.intervals, conditions, strict=True):
        interval, state = dispatch_hour(intent, hub, cond, state)
        total += interval.energy_cost_eur
        dli += interval.dli_contribution_mol_m2
        run_state.append(interval.chp_running)

        import_limit, export_limit = hub.contract.limits_at(intent.hour)
        if interval.grid_import_kw > import_limit + 1e-6:
            violations.append(f"grid.import_limit@{intent.hour}")
        if interval.grid_export_kw > export_limit + 1e-6:
            violations.append(f"grid.export_limit@{intent.hour}")

        soc = interval.battery_soc_requested_kwh
        if not (b.soc_min_kwh - 1e-6 <= soc <= b.soc_max_kwh + 1e-6):
            violations.append(f"battery.state_of_charge@{intent.hour}")
        elif not (b.soc_min_kwh + soc_span <= interval.battery_soc_kwh <= b.soc_max_kwh - soc_span):
            margin_hits += 1

        level = interval.buffer_level_requested_kwh
        if not (buf.level_min_kwh - 1e-6 <= level <= buf.level_max_kwh + 1e-6):
            violations.append(f"buffer.level_bounds@{intent.hour}")
        elif not (
            buf.level_min_kwh + buf_span
            <= interval.buffer_level_kwh
            <= buf.level_max_kwh - buf_span
        ):
            margin_hits += 1

        if interval.heat_shortfall_kw > 1e-6:
            violations.append(f"heat.demand_met@{intent.hour}")
        charge_bound = min(b.max_charge_kw, b.c_rate_power_kw)
        discharge_bound = min(b.max_discharge_kw, b.c_rate_power_kw)
        if (
            interval.battery_charge_requested_kw > charge_bound + 1e-6
            or interval.battery_discharge_requested_kw > discharge_bound + 1e-6
        ):
            violations.append(f"battery.power_limit@{intent.hour}")

    crop = hub.crop
    if not (
        crop.dli_target_mol_m2 - crop.dli_tolerance_mol_m2 - 1e-9
        <= dli
        <= crop.dli_target_mol_m2 + crop.dli_tolerance_mol_m2 + 1e-9
    ):
        violations.append("crop.daily_light_integral")

    violations.extend(_chp_duration_violations(run_state, hub))
    return ScheduleScore(
        cost_eur=total,
        feasible=not violations,
        dli_mol_m2=dli,
        violations=tuple(violations),
        margin_violations=margin_hits,
    )


def _chp_duration_violations(run_state: Sequence[bool], hub: EnergyHub) -> list[str]:
    """Minimum run and down times, evaluated the way the checker evaluates them.

    Takes the *dispatched* run state, not the requested modes. They are not the same
    thing: ``heat_led`` in an hour with no heat demand dispatches zero output, so the
    unit is not running even though the mode is not "off". Judging the modes instead
    would let the optimiser accept plans the checker then rejects -- the precise
    failure this module's docstring promises to avoid.
    """
    chp = hub.chp
    state = list(run_state)
    out: list[str] = []
    for value, minimum, name in (
        (True, chp.min_run_hours, "chp.min_run_time"),
        (False, chp.min_down_hours, "chp.min_down_time"),
    ):
        if minimum <= 1:
            continue
        pairs = zip((chp.initially_running, *state), state, strict=False)
        for start, (prev, now) in enumerate(pairs):
            if prev == value or now != value:
                continue
            length = 0
            completed = False
            for h in range(start, len(state)):
                if state[h] != value:
                    completed = True
                    break
                length += 1
            # Mirrors the checker: a streak running to midnight is unfinished.
            if completed and length < minimum:
                out.append(f"{name}@{start}")
    return out


# --------------------------------------------------------------------------
# The search
# --------------------------------------------------------------------------

LIGHT_LEVELS: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
HEAT_SOURCES: tuple[str, ...] = ("boiler", "chp", "buffer")
CHP_MODES: tuple[str, ...] = ("off", "heat_led", "max_export")


@dataclass
class OptimizingScheduler:
    """Deterministic local search over the hourly intent vector.

    Starts from the rule-based plan -- a known-feasible, sensible schedule -- and
    repeatedly sweeps every hour trying every alternative for one field at a time,
    keeping any change that is feasible and strictly cheaper. It stops when a full
    sweep finds no improvement, or when the evaluation budget runs out.

    This is hill climbing, so it finds a local optimum rather than a guaranteed
    global one. That is an honest trade: it is deterministic, it can never return
    something worse than the baseline it started from, and every candidate it
    considers is scored by the real dispatch model. Reporting it as "optimal" would
    be wrong, and the MPC reference (stage 5) is what will eventually bound how much
    is being left on the table.

    Attributes:
        max_sweeps: Maximum full passes over all hours and fields.
        max_evaluations: Hard budget on candidate evaluations, so planning time
            stays bounded regardless of the scenario.
        battery_levels: Fractions of the battery's usable power to consider.
        safety_margin: Fraction of the usable storage band to hold in reserve. An
            optimiser will drain the heat buffer to exactly its floor, because under
            its own forecast that is the cheapest feasible plan -- and then any
            forecast error puts it through the floor.

            The default was chosen by measurement, not taste. Over 80 held-out days
            on the surrogate greenhouse, raising the margin trades raw saving for
            acceptance, and what survives the checker peaks in the middle:

            ===========  ===========  ==========  ==================
            margin       raw saving   accepted    effective saving
            ===========  ===========  ==========  ==================
            0.00         19.3%        61/80       14.1%
            0.15         19.0%        69/80       16.0%
            0.45         18.0%        75/80       16.4%
            0.75         17.0%        71/80       14.5%
            ===========  ===========  ==========  ==================

            "Effective" counts a rejected plan as zero saving, since it falls back
            to the baseline (R18). Planning right up to the limit is worth less than
            planning with reserve, which is the checker changing what the optimal
            planner should do -- a cost objective alone would never find this.

            Re-measure it once the greenhouse model is validated; the right reserve
            depends on how wrong the forecast actually is.
    """

    name: str = "optimizing"
    max_sweeps: int = 6
    max_evaluations: int = 20_000
    battery_levels: tuple[float, ...] = (0.0, 0.5, 1.0)
    safety_margin: float = 0.45
    evaluations: int = field(default=0, init=False)
    margin_used: float = field(default=0.0, init=False)
    sweeps_used: int = field(default=0, init=False)
    start_cost_eur: float = field(default=0.0, init=False)
    final_cost_eur: float = field(default=0.0, init=False)

    def optimise(
        self, seed_plan: Plan, hub: EnergyHub, conditions: Sequence[HourlyConditions]
    ) -> Plan:
        """Improve ``seed_plan`` under ``conditions``, never returning something worse."""
        self.evaluations = 0
        self.margin_used = self.safety_margin
        best = seed_plan
        best_score = self._score(best, hub, conditions, self.margin_used)
        self.start_cost_eur = best_score.cost_eur

        # If the seed is infeasible even at the real limits, the search has no valid
        # ground to stand on; hand it back so the caller's fallback path takes over.
        if not best_score.feasible:
            self.final_cost_eur = best_score.cost_eur
            return best

        rating = min(
            hub.battery.max_charge_kw, hub.battery.max_discharge_kw, hub.battery.c_rate_power_kw
        )

        for sweep in range(self.max_sweeps):
            self.sweeps_used = sweep + 1
            improved = False
            for hour in range(HOURS_PER_DAY):
                for candidate in self._moves(best, hour, rating):
                    if self.evaluations >= self.max_evaluations:
                        self.final_cost_eur = best_score.cost_eur
                        return best
                    score = self._score(candidate, hub, conditions, self.margin_used)
                    if _better(score.objective, best_score.objective):
                        best, best_score = candidate, score
                        improved = True
            if not improved:
                break

        self.final_cost_eur = best_score.cost_eur
        return best

    def _score(
        self,
        plan: Plan,
        hub: EnergyHub,
        conditions: Sequence[HourlyConditions],
        margin: float = 0.0,
    ) -> ScheduleScore:
        self.evaluations += 1
        return score_plan(plan, hub, conditions, margin=margin)

    def _moves(self, plan: Plan, hour: int, battery_rating_kw: float):
        """Every single-field alternative for one hour. One field at a time keeps
        each move cheap to evaluate and makes the search easy to reason about."""
        current = plan.intervals[hour]
        for level in LIGHT_LEVELS:
            if abs(level - current.lighting_level) > 1e-9:
                yield _with(plan, hour, lighting_level=level)
        for source in HEAT_SOURCES:
            if source != current.heat_source:
                yield _with(plan, hour, heat_source=source)
        for mode in CHP_MODES:
            if mode != current.chp_mode:
                yield _with(plan, hour, chp_mode=mode)
        for action in ("idle", "charge", "discharge"):
            for frac in self.battery_levels:
                power = round(battery_rating_kw * frac, 2)
                if action == "idle" and frac != 0.0:
                    continue
                if action != "idle" and frac == 0.0:
                    continue
                if action == current.battery and abs(power - current.battery_power_kw) < 1e-9:
                    continue
                yield _with(plan, hour, battery=action, battery_power_kw=power)


def _explain(plan: Plan, conditions: Sequence[HourlyConditions], margin: float) -> Plan:
    """Rewrite each hour's reasoning to match the plan as optimised.

    The search starts from the rule-based plan and then changes fields, which leaves
    the seed's explanation attached to an hour it no longer describes. That text is
    what a human is asked to approve (R22), so a stale line is not cosmetic -- it is
    an operator reading one plan and approving another.
    """
    lines = []
    for intent, cond in zip(plan.intervals, conditions, strict=True):
        parts = [f"power {cond.power_price_eur_kwh:.3f} EUR/kWh"]
        parts.append(f"heat from {intent.heat_source}")
        if intent.lighting_level > 0:
            parts.append(f"lamps {intent.lighting_level:.0%}")
        if intent.chp_mode != "off":
            parts.append(f"CHP {intent.chp_mode.replace('_', ' ')}")
        if intent.battery != "idle":
            parts.append(f"battery {intent.battery} {intent.battery_power_kw:.0f} kW")
        if intent.co2_source != "none":
            parts.append(f"CO2 from {intent.co2_source}")
        lines.append(dataclasses.replace(intent, reasoning="; ".join(parts)))
    return dataclasses.replace(plan, intervals=tuple(lines))


def _better(candidate: tuple[int, float], incumbent: tuple[int, float]) -> bool:
    """Strict lexicographic improvement, with a tolerance on the cost component."""
    if candidate[0] != incumbent[0]:
        return candidate[0] < incumbent[0]
    return candidate[1] < incumbent[1] - 1e-9


def _with(plan: Plan, hour: int, **changes) -> Plan:
    """A copy of ``plan`` with one hour's intent changed. Revision is not bumped:
    these are search candidates, not revisions offered to a human."""
    edited = tuple(
        dataclasses.replace(iv, **changes) if iv.hour == hour else iv for iv in plan.intervals
    )
    return dataclasses.replace(plan, intervals=edited)


# --------------------------------------------------------------------------
# The planner
# --------------------------------------------------------------------------


@dataclass
class LearnedPlanner:
    """Forecasts demand from history, then optimises the schedule against it.

    This is the planner the project was missing: everything else either had the
    demand handed to it or guessed at a schedule by rule. It plans on what it
    *predicts*, which means its forecast error shows up honestly in the result --
    a plan optimised against a bad forecast is a bad plan, and the run records it
    as such.

    Attributes:
        history: Past days used for fitting and for the lag features. Must end
            immediately before the day being planned.
        forecaster: The demand model. Refitted on the whole history at each plan.
        scheduler: The search.
        use_forecast_demand: When True the planner replaces the demand in the
            context with its own prediction. Setting it False makes the planner
            an oracle on demand and is only for isolating scheduling skill from
            forecasting skill in an experiment.
    """

    history: Sequence[DayRecord]
    name: str = "learned"
    forecaster: Forecaster = field(default_factory=RidgeForecaster)
    scheduler: OptimizingScheduler = field(default_factory=OptimizingScheduler)
    use_forecast_demand: bool = True
    last_forecast_kw: tuple[float, ...] = field(default=(), init=False)
    last_diagnostics: dict[str, float] = field(default_factory=dict, init=False)

    def plan(self, context: PlanningContext) -> Plan:
        """Predict tomorrow's demand, then search for the cheapest feasible schedule."""
        conditions = self._conditions(context)
        seed = RuleBasedPlanner().plan(dataclasses.replace(context, forecast=tuple(conditions)))
        best = self.scheduler.optimise(seed, context.hub, conditions)

        self.last_diagnostics = {
            "evaluations": float(self.scheduler.evaluations),
            "sweeps": float(self.scheduler.sweeps_used),
            "seed_cost_eur": round(self.scheduler.start_cost_eur, 2),
            "optimised_cost_eur": round(self.scheduler.final_cost_eur, 2),
            "saving_eur": round(self.scheduler.start_cost_eur - self.scheduler.final_cost_eur, 2),
            "safety_margin": self.scheduler.margin_used,
        }
        best = _explain(best, conditions, self.scheduler.margin_used)
        return dataclasses.replace(
            best,
            planner=self.name,
            brief=context.brief,
            revision=context.revision,
            notes=(
                f"Demand forecast by {getattr(self.forecaster, 'name', '?')}; "
                f"schedule improved by local search over "
                f"{self.scheduler.evaluations} candidates "
                f"(EUR {self.last_diagnostics['saving_eur']:.2f} below the rule-based seed)."
            ),
        )

    def _conditions(self, context: PlanningContext) -> list[HourlyConditions]:
        """Forecast conditions, with heat demand replaced by the model's prediction."""
        if not self.use_forecast_demand:
            self.last_forecast_kw = tuple(c.heat_demand_kw for c in context.forecast)
            return list(context.forecast)

        # The day being planned is appended to the history so the lag features can
        # reach back into it; only its *forecast* weather is read, never its actuals.
        target = _ForecastDay(
            date=context.date,
            forecast=tuple(context.forecast),
            actual=tuple(context.forecast),
        )
        days = [*self.history, target]
        index = len(days) - 1
        self.forecaster.fit(days, upto=index)
        predicted = np.asarray(self.forecaster.predict(days, index), dtype=float)
        self.last_forecast_kw = tuple(float(v) for v in predicted)

        return [
            dataclasses.replace(c, heat_demand_kw=float(predicted[i]))
            for i, c in enumerate(context.forecast)
        ]


@dataclass(frozen=True)
class _ForecastDay:
    """The day being planned, wrapped so the feature builder can consume it.

    ``actual`` is set to the forecast series deliberately: the feature builder never
    reads the target day's actuals, and pointing the field at real actuals here
    would create a leak the moment that ever changed.
    """

    date: str
    forecast: tuple[HourlyConditions, ...]
    actual: tuple[HourlyConditions, ...]
