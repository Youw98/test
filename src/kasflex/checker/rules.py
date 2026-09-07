"""The safety checks themselves.

Every check is a small pure function registered in :data:`CHECK_REGISTRY` under a
stable name. Registering them individually is what makes R19 and R21 cheap: the
checker can be switched off wholesale, or a single named check can be excluded so
the study can measure what goes wrong when one outcome is left unverified.

Adding a check is deliberately a three-line job -- write the function, decorate it,
add a test with a deliberately invalid plan (R20). Nothing else has to change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from kasflex.checker.verdict import Severity, Verdict, Violation
from kasflex.energy.assets import EnergyHub
from kasflex.energy.dispatch import DispatchResult, HourlyConditions, dispatch_plan
from kasflex.intent import Plan

CheckFn = Callable[["CheckContext"], list[Violation]]
CHECK_REGISTRY: dict[str, CheckFn] = {}


@dataclass(frozen=True)
class CheckContext:
    """Everything a check may look at."""

    plan: Plan
    hub: EnergyHub
    conditions: tuple[HourlyConditions, ...]
    dispatch: DispatchResult
    projection: dict[str, list[float]] | None = None
    """Optional forward projection of indoor climate from the greenhouse adapter,
    keyed by ``"temp_c"``, ``"rh_pct"``, ``"co2_ppm"``, one value per hour. When
    absent, the projected crop-climate checks are skipped rather than guessed."""


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def decorator(fn: CheckFn) -> CheckFn:
        if name in CHECK_REGISTRY:
            raise ValueError(f"duplicate check name {name!r}")
        CHECK_REGISTRY[name] = fn
        return fn

    return decorator


# --------------------------------------------------------------------------
# Electrical limits (R14)
# --------------------------------------------------------------------------


@check("grid.import_limit")
def _grid_import(ctx: CheckContext) -> list[Violation]:
    out = []
    for iv in ctx.dispatch.intervals:
        import_limit, _ = ctx.hub.contract.limits_at(iv.hour)
        if iv.grid_import_kw > import_limit + 1e-6:
            out.append(
                Violation(
                    constraint="grid.import_limit",
                    category="electrical",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.grid_import_kw,
                    bound=import_limit,
                    unit="kW",
                    message=(
                        f"Offtake exceeds the connection contract by "
                        f"{iv.grid_import_kw - import_limit:.0f} kW. Reduce lighting, "
                        f"stop charging the battery, or run the CHP in this hour."
                    ),
                )
            )
    return out


@check("grid.export_limit")
def _grid_export(ctx: CheckContext) -> list[Violation]:
    out = []
    for iv in ctx.dispatch.intervals:
        _, export_limit = ctx.hub.contract.limits_at(iv.hour)
        if iv.grid_export_kw > export_limit + 1e-6:
            out.append(
                Violation(
                    constraint="grid.export_limit",
                    category="electrical",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.grid_export_kw,
                    bound=export_limit,
                    unit="kW",
                    message=(
                        f"Feed-in exceeds the connection contract by "
                        f"{iv.grid_export_kw - export_limit:.0f} kW. Lower the CHP "
                        f"setpoint or absorb the surplus by charging the battery."
                    ),
                )
            )
    return out


@check("battery.power_limit")
def _battery_power(ctx: CheckContext) -> list[Violation]:
    b = ctx.hub.battery
    out = []
    for iv in ctx.dispatch.intervals:
        for power, rating, direction in (
            (iv.battery_charge_kw, b.max_charge_kw, "charge"),
            (iv.battery_discharge_kw, b.max_discharge_kw, "discharge"),
        ):
            if power <= 1e-9:
                continue
            # Report whichever of the two limits actually binds.
            bound, which = (rating, f"{direction} rating")
            if b.c_rate_power_kw < rating:
                bound, which = (b.c_rate_power_kw, f"{b.c_rate_max:g}C rate limit")
            if power > bound + 1e-6:
                out.append(
                    Violation(
                        constraint="battery.power_limit",
                        category="electrical",
                        severity=Severity.HARD,
                        hour=iv.hour,
                        actual=power,
                        bound=bound,
                        unit="kW",
                        message=(
                            f"Battery {direction} request exceeds the {which}. "
                            f"Request at most {bound:.0f} kW in this hour."
                        ),
                    )
                )
    return out


@check("battery.state_of_charge")
def _battery_soc(ctx: CheckContext) -> list[Violation]:
    b = ctx.hub.battery
    out = []
    for iv in ctx.dispatch.intervals:
        if iv.battery_soc_kwh < b.soc_min_kwh - 1e-6:
            out.append(
                Violation(
                    constraint="battery.state_of_charge",
                    category="electrical",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.battery_soc_kwh,
                    bound=b.soc_min_kwh,
                    unit="kWh",
                    message=(
                        "State of charge falls below the floor. Discharge less in this "
                        "hour or charge earlier in the day."
                    ),
                )
            )
        elif iv.battery_soc_kwh > b.soc_max_kwh + 1e-6:
            out.append(
                Violation(
                    constraint="battery.state_of_charge",
                    category="electrical",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.battery_soc_kwh,
                    bound=b.soc_max_kwh,
                    unit="kWh",
                    message=(
                        "State of charge exceeds the ceiling. Charge less in this hour "
                        "or discharge earlier in the day."
                    ),
                )
            )
    return out


# --------------------------------------------------------------------------
# Asset limits (R15)
# --------------------------------------------------------------------------


@check("buffer.level_bounds")
def _buffer_level(ctx: CheckContext) -> list[Violation]:
    buf = ctx.hub.buffer
    out = []
    for iv in ctx.dispatch.intervals:
        if iv.buffer_level_kwh < buf.level_min_kwh - 1e-6:
            out.append(
                Violation(
                    constraint="buffer.level_bounds",
                    category="asset",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.buffer_level_kwh,
                    bound=buf.level_min_kwh,
                    unit="kWh",
                    message=(
                        "The heat buffer is drawn below its minimum level. Cover this "
                        "hour from the boiler or the CHP, or charge the buffer earlier."
                    ),
                )
            )
        elif iv.buffer_level_kwh > buf.level_max_kwh + 1e-6:
            out.append(
                Violation(
                    constraint="buffer.level_bounds",
                    category="asset",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.buffer_level_kwh,
                    bound=buf.level_max_kwh,
                    unit="kWh",
                    message=(
                        "The heat buffer overflows. Run the CHP at a lower setpoint or "
                        "draw heat from the buffer earlier in the day."
                    ),
                )
            )
    return out


@check("chp.min_run_time")
def _chp_min_run(ctx: CheckContext) -> list[Violation]:
    chp = ctx.hub.chp
    if chp.min_run_hours <= 1:
        return []
    out = []
    state = ctx.dispatch.chp_run_state
    for start, (prev, now) in enumerate(zip((chp.initially_running, *state), state, strict=False)):
        if prev or not now:
            continue
        run_length = 0
        completed = False
        for h in range(start, len(state)):
            if not state[h]:
                completed = True
                break
            run_length += 1
        # A run still going at midnight is not short -- it is unfinished. Its length
        # depends on tomorrow's plan, which this plan does not contain, so judging it
        # here would reject a perfectly ordinary overnight run for ending at the edge
        # of the horizon. Only completed runs are decidable.
        if completed and run_length < chp.min_run_hours:
            out.append(
                Violation(
                    constraint="chp.min_run_time",
                    category="asset",
                    severity=Severity.HARD,
                    hour=start,
                    actual=float(run_length),
                    bound=float(chp.min_run_hours),
                    unit="h",
                    message=(
                        f"The CHP is started at hour {start:02d} and stopped after "
                        f"{run_length} h, short of its {chp.min_run_hours} h minimum "
                        f"run time. Either keep it running longer or leave it off."
                    ),
                )
            )
    return out


@check("chp.min_down_time")
def _chp_min_down(ctx: CheckContext) -> list[Violation]:
    chp = ctx.hub.chp
    if chp.min_down_hours <= 1:
        return []
    out = []
    state = ctx.dispatch.chp_run_state
    for start, (prev, now) in enumerate(zip((chp.initially_running, *state), state, strict=False)):
        if not prev or now:
            continue
        down_length = 0
        completed = False
        for h in range(start, len(state)):
            if state[h]:
                completed = True
                break
            down_length += 1
        # As with the minimum run time: an off-period that runs to midnight is
        # unfinished, not too short.
        if completed and down_length < chp.min_down_hours:
            out.append(
                Violation(
                    constraint="chp.min_down_time",
                    category="asset",
                    severity=Severity.HARD,
                    hour=start,
                    actual=float(down_length),
                    bound=float(chp.min_down_hours),
                    unit="h",
                    message=(
                        f"The CHP is stopped at hour {start:02d} and restarted after "
                        f"{down_length} h, short of its {chp.min_down_hours} h minimum "
                        f"down time. Keep it off longer or do not stop it here."
                    ),
                )
            )
    return out


@check("chp.ramp_rate")
def _chp_ramp(ctx: CheckContext) -> list[Violation]:
    """Flag a plan that asks for a CHP setpoint change the unit cannot physically make.

    Dispatch clips the ramp, so the realised trajectory is always feasible. What is
    checked here is the *request*: a plan that asks for an impossible swing has not
    understood the asset, and the operator should see that rather than silently get
    something other than what was planned.
    """
    chp = ctx.hub.chp
    out = []
    previous = chp.electrical_capacity_kw if chp.initially_running else 0.0
    for intent, cond in zip(ctx.plan.intervals, ctx.conditions, strict=True):
        if intent.chp_mode == "off":
            requested = 0.0
        elif intent.chp_mode == "max_export":
            requested = chp.electrical_capacity_kw
        else:
            implied = (
                cond.heat_demand_kw / chp.heat_to_power_ratio
                if chp.heat_to_power_ratio > 0
                else 0.0
            )
            requested = (
                0.0 if implied <= 0.0
                else min(chp.electrical_capacity_kw, max(chp.min_load_kw, implied))
            )
        change = abs(requested - previous)
        if change > chp.ramp_kw_per_hour + 1e-6:
            out.append(
                Violation(
                    constraint="chp.ramp_rate",
                    category="asset",
                    severity=Severity.HARD,
                    hour=intent.hour,
                    actual=change,
                    bound=chp.ramp_kw_per_hour,
                    unit="kW/h",
                    message=(
                        "The requested CHP setpoint change is faster than the unit can "
                        "ramp. Spread the change over more hours."
                    ),
                )
            )
        # Follow the realised (ramped) trajectory, so one impossible step does not
        # cascade into a violation reported in every later hour.
        previous = ctx.dispatch.intervals[intent.hour].chp_electrical_kw
    return out


@check("heat.demand_met")
def _heat_met(ctx: CheckContext) -> list[Violation]:
    out = []
    for iv in ctx.dispatch.intervals:
        if iv.heat_shortfall_kw > 1e-6:
            out.append(
                Violation(
                    constraint="heat.demand_met",
                    category="asset",
                    severity=Severity.HARD,
                    hour=iv.hour,
                    actual=iv.heat_delivered_kw,
                    bound=iv.heat_demand_kw,
                    unit="kW",
                    message=(
                        f"Heat demand is not covered; {iv.heat_shortfall_kw:.0f} kW is "
                        f"missing. Add the boiler as a heat source in this hour."
                    ),
                )
            )
    return out


# --------------------------------------------------------------------------
# Crop limits (R16)
# --------------------------------------------------------------------------


@check("crop.daily_light_integral")
def _crop_dli(ctx: CheckContext) -> list[Violation]:
    """The one crop check that is exactly decidable before execution.

    The daily light integral follows from the lighting plan and the lamp
    specification alone, so it is HARD. Natural light is deliberately excluded:
    the supplemental contribution is what the planner controls, and the target is
    stated on the same basis.
    """
    crop = ctx.hub.crop
    total = ctx.dispatch.total_dli_mol_m2
    low = crop.dli_target_mol_m2 - crop.dli_tolerance_mol_m2
    high = crop.dli_target_mol_m2 + crop.dli_tolerance_mol_m2
    if total < low:
        return [
            Violation(
                constraint="crop.daily_light_integral",
                category="crop",
                severity=Severity.HARD,
                hour=None,
                actual=total,
                bound=low,
                unit="mol/m2/day",
                message=(
                    f"Supplemental light over the day is {low - total:.1f} mol/m2 short "
                    f"of the crop minimum. Raise the lighting level or extend the "
                    f"lit hours."
                ),
            )
        ]
    if total > high:
        return [
            Violation(
                constraint="crop.daily_light_integral",
                category="crop",
                severity=Severity.HARD,
                hour=None,
                actual=total,
                bound=high,
                unit="mol/m2/day",
                message=(
                    f"Supplemental light over the day exceeds the crop maximum by "
                    f"{total - high:.1f} mol/m2. Dim the lamps or shorten the lit hours."
                ),
            )
        ]
    return []


def _projected_band(
    ctx: CheckContext,
    constraint: str,
    key: str,
    name: str,
    low: float | None,
    high: float | None,
    unit: str,
) -> list[Violation]:
    """Test a projected hourly series against a band.

    ``constraint`` is passed in rather than derived from ``key`` so that the emitted
    constraint id is exactly the name the check is registered under. Analysis code
    matches ``violation.constraint`` against ``verdict.checks_excluded``, and those
    two vocabularies drifting apart would make an excluded check impossible to
    account for.
    """
    if not ctx.projection or key not in ctx.projection:
        return []
    out = []
    for hour, value in enumerate(ctx.projection[key]):
        if low is not None and value < low - 1e-6:
            out.append(
                Violation(
                    constraint=constraint,
                    category="crop",
                    severity=Severity.PROJECTED,
                    hour=hour,
                    actual=float(value),
                    bound=low,
                    unit=unit,
                    message=f"Projected {name} falls below the crop band.",
                )
            )
        elif high is not None and value > high + 1e-6:
            out.append(
                Violation(
                    constraint=constraint,
                    category="crop",
                    severity=Severity.PROJECTED,
                    hour=hour,
                    actual=float(value),
                    bound=high,
                    unit=unit,
                    message=f"Projected {name} exceeds the crop band.",
                )
            )
    return out


@check("crop.temperature_band")
def _crop_temp(ctx: CheckContext) -> list[Violation]:
    c = ctx.hub.crop
    return _projected_band(
        ctx, "crop.temperature_band", "temp_c", "air temperature",
        c.temp_min_c, c.temp_max_c, "degC",
    )


@check("crop.humidity")
def _crop_rh(ctx: CheckContext) -> list[Violation]:
    return _projected_band(
        ctx, "crop.humidity", "rh_pct", "relative humidity",
        None, ctx.hub.crop.rh_max_pct, "%",
    )


@check("crop.co2")
def _crop_co2(ctx: CheckContext) -> list[Violation]:
    c = ctx.hub.crop
    return _projected_band(
        ctx, "crop.co2", "co2_ppm", "CO2 concentration",
        c.co2_min_ppm, c.co2_max_ppm, "ppm",
    )


# --------------------------------------------------------------------------
# The checker
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckerConfig:
    """How the checker behaves in a given experimental condition (R19, R21).

    Attributes:
        enabled: When False the checker accepts everything without running a single
            check. This is the "checker disabled" arm of the headline comparison.
        explain: When False a rejection carries no reasons (see
            :meth:`Verdict.feedback`).
        excluded_checks: Names of checks to skip while otherwise verifying normally.
            R21 requires at least one designated outcome to be left unverified so
            that unchecked degradation can be measured; the excluded names are
            recorded in every verdict so a result can never be read as fully
            verified by mistake.
        max_revisions: How many times a planner may revise after a rejection before
            control is handed to the rule-based baseline (R18).
        fail_on_projected: When False, PROJECTED violations are reported but do not
            by themselves reject the plan. Default False, because rejecting on a
            model's forward guess would attribute the greenhouse model's error to
            the planner.
    """

    enabled: bool = True
    explain: bool = True
    excluded_checks: tuple[str, ...] = ()
    max_revisions: int = 3
    fail_on_projected: bool = False

    def __post_init__(self) -> None:
        unknown = set(self.excluded_checks) - set(CHECK_REGISTRY)
        if unknown:
            raise ValueError(
                f"unknown check name(s) in excluded_checks: {sorted(unknown)}; "
                f"available checks are {sorted(CHECK_REGISTRY)}"
            )


@dataclass(frozen=True)
class SafetyChecker:
    """Verifies a plan against electrical, asset and crop limits.

    The checker is pure: it never mutates the plan, never calls out to a model it
    does not own, and returns the same verdict for the same inputs. That is what
    lets a reviewer re-derive any published verdict from the logged plan alone.
    """

    hub: EnergyHub
    config: CheckerConfig = field(default_factory=CheckerConfig)

    def active_checks(self) -> tuple[str, ...]:
        return tuple(n for n in CHECK_REGISTRY if n not in self.config.excluded_checks)

    def verify(
        self,
        plan: Plan,
        conditions: Iterable[HourlyConditions],
        projection: dict[str, list[float]] | None = None,
    ) -> Verdict:
        """Verify ``plan`` and return a :class:`Verdict`.

        Args:
            plan: The plan to verify.
            conditions: The 24 hourly conditions the plan will run under. These must
                be *forecasts*, never actuals -- see R4 and ADR-0005.
            projection: Optional forward projection of indoor climate, enabling the
                PROJECTED crop checks.
        """
        conds = tuple(conditions)
        if not self.config.enabled:
            return Verdict(
                accepted=True,
                enabled=False,
                plan_revision=plan.revision,
                checks_excluded=tuple(sorted(CHECK_REGISTRY)),
                metadata={"note": "checker disabled; plan executed unverified"},
            )

        dispatch = dispatch_plan(plan, self.hub, list(conds))
        ctx = CheckContext(
            plan=plan, hub=self.hub, conditions=conds, dispatch=dispatch, projection=projection
        )

        violations: list[Violation] = []
        run: list[str] = []
        for name in CHECK_REGISTRY:
            if name in self.config.excluded_checks:
                continue
            run.append(name)
            violations.extend(CHECK_REGISTRY[name](ctx))

        violations.sort(key=lambda v: (v.hour if v.hour is not None else -1, v.constraint))
        blocking = [
            v
            for v in violations
            if v.severity is Severity.HARD or self.config.fail_on_projected
        ]
        return Verdict(
            accepted=not blocking,
            violations=tuple(violations),
            checks_run=tuple(run),
            checks_excluded=tuple(self.config.excluded_checks),
            enabled=True,
            plan_revision=plan.revision,
            metadata={"n_blocking": len(blocking)},
        )
