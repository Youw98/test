"""R20: the checker is covered by tests using deliberately invalid plans.

Each test constructs a plan that breaks exactly one limit and asserts that the
checker names that limit, in the right interval, with the right bound. A checker
that rejects for the wrong reason is no more useful to a planner than one that
does not reject at all.
"""

from __future__ import annotations

import dataclasses

import pytest

from kasflex.checker.rules import CHECK_REGISTRY, CheckerConfig, SafetyChecker
from kasflex.checker.verdict import Severity
from kasflex.energy.assets import ContractLimits, EnergyHub
from kasflex.intent import flat_plan


def constraints(verdict) -> set[str]:
    return {v.constraint for v in verdict.violations}


def test_compliant_plan_is_accepted(hub, conditions):
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63)
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert verdict.accepted, verdict.feedback()


def test_grid_import_limit(hub, conditions):
    """Full lighting on a 5 ha lamp field will not fit a small connection."""
    small = dataclasses.replace(hub, contract=ContractLimits(import_limit_kw=500,
                                                             export_limit_kw=500))
    plan = flat_plan("d", heat_source="boiler", lighting_level=1.0)
    verdict = SafetyChecker(small).verify(plan, conditions)
    assert not verdict.accepted
    assert "grid.import_limit" in constraints(verdict)
    breach = next(v for v in verdict.violations if v.constraint == "grid.import_limit")
    assert breach.bound == 500
    assert breach.actual > 500
    assert breach.severity is Severity.HARD


def test_congestion_window_is_tighter_than_the_base_contract(hub, conditions):
    contract = ContractLimits(
        import_limit_kw=6000, export_limit_kw=4000, congestion_windows={17: (100.0, 100.0)}
    )
    constrained = dataclasses.replace(hub, contract=contract)
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.5)
    verdict = SafetyChecker(constrained).verify(plan, conditions)
    breaches = [v for v in verdict.violations if v.constraint == "grid.import_limit"]
    assert breaches, "the congestion window should bind"
    assert all(v.hour == 17 for v in breaches)


def test_grid_export_limit(hub, conditions):
    tight = dataclasses.replace(hub, contract=ContractLimits(import_limit_kw=6000,
                                                             export_limit_kw=10))
    plan = flat_plan("d", heat_source="chp", chp_mode="max_export", lighting_level=0.0)
    verdict = SafetyChecker(tight).verify(plan, conditions)
    assert "grid.export_limit" in constraints(verdict)


def test_battery_power_limit(hub, conditions):
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63,
                     battery="charge", battery_power_kw=99_000.0)
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert "battery.power_limit" in constraints(verdict)


def test_battery_state_of_charge_floor(hub, conditions):
    """Discharging every hour must drain the battery through its floor."""
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63,
                     battery="discharge", battery_power_kw=hub.battery.max_discharge_kw)
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert "battery.state_of_charge" in constraints(verdict)
    breach = next(v for v in verdict.violations if v.constraint == "battery.state_of_charge")
    assert breach.actual < breach.bound


def test_buffer_drawn_below_floor(hub, conditions):
    plan = flat_plan("d", heat_source="buffer", lighting_level=0.63)
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert "buffer.level_bounds" in constraints(verdict)


def test_chp_min_run_time():
    """A one-hour CHP run breaches a two-hour minimum."""
    from kasflex.energy.dispatch import HourlyConditions

    hub = EnergyHub()
    conds = tuple(HourlyConditions(hour=h, heat_demand_kw=1500.0) for h in range(24))
    intervals = []
    from kasflex.intent import IntervalIntent, Plan

    for h in range(24):
        on = h == 5
        intervals.append(
            IntervalIntent(hour=h, heat_source="chp" if on else "boiler",
                           chp_mode="max_export" if on else "off", lighting_level=0.63)
        )
    verdict = SafetyChecker(hub).verify(Plan(date="d", intervals=tuple(intervals)), conds)
    assert "chp.min_run_time" in constraints(verdict)
    breach = next(v for v in verdict.violations if v.constraint == "chp.min_run_time")
    assert breach.hour == 5
    assert breach.actual == 1.0
    assert breach.bound == hub.chp.min_run_hours


def test_chp_min_down_time():
    from kasflex.energy.dispatch import HourlyConditions
    from kasflex.intent import IntervalIntent, Plan

    hub = dataclasses.replace(EnergyHub(), chp=dataclasses.replace(
        EnergyHub().chp, initially_running=True, min_down_hours=3))
    conds = tuple(HourlyConditions(hour=h, heat_demand_kw=1500.0) for h in range(24))
    # Off for one hour, then straight back on: too short a gap.
    intervals = [
        IntervalIntent(hour=h, heat_source="chp" if h != 0 else "boiler",
                       chp_mode="off" if h == 0 else "max_export", lighting_level=0.63)
        for h in range(24)
    ]
    verdict = SafetyChecker(hub).verify(Plan(date="d", intervals=tuple(intervals)), conds)
    assert "chp.min_down_time" in constraints(verdict)


def test_chp_ramp_rate():
    from kasflex.energy.dispatch import HourlyConditions
    from kasflex.intent import IntervalIntent, Plan

    slow = dataclasses.replace(EnergyHub(), chp=dataclasses.replace(
        EnergyHub().chp, ramp_kw_per_hour=100.0))
    conds = tuple(HourlyConditions(hour=h, heat_demand_kw=1500.0) for h in range(24))
    intervals = [
        IntervalIntent(hour=h, heat_source="chp", chp_mode="max_export", lighting_level=0.63)
        for h in range(24)
    ]
    verdict = SafetyChecker(slow).verify(Plan(date="d", intervals=tuple(intervals)), conds)
    assert "chp.ramp_rate" in constraints(verdict)


def test_heat_demand_not_met(hub, conditions):
    plan = flat_plan("d", heat_source="none", lighting_level=0.63)
    verdict = SafetyChecker(hub).verify(plan, conditions)
    assert "heat.demand_met" in constraints(verdict)


def test_dli_too_low_and_too_high(hub, conditions):
    dark = SafetyChecker(hub).verify(flat_plan("d", heat_source="boiler",
                                               lighting_level=0.0), conditions)
    assert "crop.daily_light_integral" in constraints(dark)
    bright = SafetyChecker(hub).verify(flat_plan("d", heat_source="boiler",
                                                 lighting_level=1.0), conditions)
    breach = next(v for v in bright.violations if v.constraint == "crop.daily_light_integral")
    assert breach.actual > breach.bound


def test_projected_crop_checks_need_a_projection(hub, conditions):
    """Without a projection the climate-band checks are skipped, never guessed."""
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63)
    without = SafetyChecker(hub).verify(plan, conditions)
    assert not [v for v in without.violations if v.constraint == "crop.temperature_band"]

    with_projection = SafetyChecker(hub).verify(
        plan, conditions, projection={"temp_c": [3.0] * 24, "rh_pct": [50.0] * 24,
                                      "co2_ppm": [800.0] * 24}
    )
    breaches = [v for v in with_projection.violations if v.constraint == "crop.temperature_band"]
    assert breaches
    assert all(v.severity is Severity.PROJECTED for v in breaches)


def test_projected_humidity_and_co2_bands(hub, conditions):
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63)
    verdict = SafetyChecker(hub, CheckerConfig(fail_on_projected=True)).verify(
        plan,
        conditions,
        projection={"temp_c": [20.0] * 24, "rh_pct": [99.0] * 24, "co2_ppm": [5000.0] * 24},
    )
    assert "crop.humidity" in constraints(verdict)
    assert "crop.co2" in constraints(verdict)
    assert not verdict.accepted


def test_constraint_ids_match_registered_check_names(hub, conditions):
    """Analysis matches violation.constraint against checks_excluded, so they must agree."""
    plan = flat_plan("d", heat_source="none", lighting_level=1.0,
                     battery="discharge", battery_power_kw=99_000.0)
    verdict = SafetyChecker(hub).verify(
        plan, conditions,
        projection={"temp_c": [3.0] * 24, "rh_pct": [99.0] * 24, "co2_ppm": [5000.0] * 24},
    )
    assert constraints(verdict) <= set(CHECK_REGISTRY)


def test_projected_violations_do_not_reject_by_default(hub, conditions):
    """A forward model's guess must not be attributed to the planner as a failure."""
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63)
    projection = {"temp_c": [3.0] * 24, "rh_pct": [50.0] * 24, "co2_ppm": [800.0] * 24}
    lenient = SafetyChecker(hub).verify(plan, conditions, projection)
    assert lenient.accepted
    assert lenient.projected_violations

    strict = SafetyChecker(hub, CheckerConfig(fail_on_projected=True)).verify(
        plan, conditions, projection
    )
    assert not strict.accepted


# --- R19 and R21: switchability -------------------------------------------


def test_disabled_checker_accepts_everything(hub, conditions):
    plan = flat_plan("d", heat_source="none", lighting_level=1.0,
                     battery="discharge", battery_power_kw=99_000.0)
    verdict = SafetyChecker(hub, CheckerConfig(enabled=False)).verify(plan, conditions)
    assert verdict.accepted
    assert not verdict.enabled
    assert verdict.violations == ()


def test_explanation_can_be_withheld_independently(hub, conditions):
    plan = flat_plan("d", heat_source="none", lighting_level=0.0)
    verdict = SafetyChecker(hub, CheckerConfig(explain=False)).verify(plan, conditions)
    assert not verdict.accepted
    assert "rejected" in verdict.feedback(explain=False).lower()
    assert "heat.demand_met" not in verdict.feedback(explain=False)
    assert "heat.demand_met" in verdict.feedback(explain=True)


def test_excluded_check_is_not_run_and_is_recorded(hub, conditions):
    """R21: an excluded check must be skipped AND declared, so no run reads as fully verified."""
    small = dataclasses.replace(hub, contract=ContractLimits(import_limit_kw=500,
                                                             export_limit_kw=500))
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.63)
    config = CheckerConfig(excluded_checks=("grid.import_limit",))
    verdict = SafetyChecker(small, config).verify(plan, conditions)
    assert "grid.import_limit" not in constraints(verdict)
    assert "grid.import_limit" not in verdict.checks_run
    assert "grid.import_limit" in verdict.checks_excluded


def test_unknown_excluded_check_is_rejected():
    with pytest.raises(ValueError, match="unknown check name"):
        CheckerConfig(excluded_checks=("grid.teleport_limit",))


def test_every_registered_check_has_a_test():
    """Guard against a check being added without a deliberately invalid plan for it.

    Crude but effective: adding a check to the registry without naming it anywhere
    in this file fails the suite, which is the cheapest possible enforcement of R20.
    """
    source = open(__file__).read()
    missing = [name for name in CHECK_REGISTRY if f'"{name}"' not in source]
    assert not missing, (
        f"checks registered with no test in this file: {missing}. R20 requires every "
        f"check to be covered by a deliberately invalid plan."
    )
