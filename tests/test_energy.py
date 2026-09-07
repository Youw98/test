"""Energy hub and dispatch: conservation, faithfulness, and physical limits."""

from __future__ import annotations

import dataclasses

from kasflex.energy.assets import Battery, EnergyHub, HeatBuffer
from kasflex.energy.dispatch import HourlyConditions, dispatch_plan
from kasflex.intent import flat_plan


def test_electrical_balance_closes_every_hour(hub, conditions):
    """Supply minus demand must equal net grid exchange, to the watt."""
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.5,
                     battery="charge", battery_power_kw=400.0)
    result = dispatch_plan(plan, hub, list(conditions))
    for iv in result.intervals:
        demand = iv.base_load_kw + iv.lighting_kw + iv.battery_charge_kw
        supply = iv.pv_kw + iv.chp_electrical_kw + iv.battery_discharge_kw
        assert abs((demand - supply) - iv.grid_net_kw) < 1e-6


def test_heat_balance_closes_every_hour(hub, conditions):
    plan = flat_plan("d", heat_source="chp", chp_mode="heat_led", lighting_level=0.3)
    result = dispatch_plan(plan, hub, list(conditions))
    for iv in result.intervals:
        chp_heat_used = min(iv.chp_heat_kw, iv.heat_demand_kw)
        delivered = chp_heat_used + iv.boiler_heat_kw + iv.buffer_discharge_kw
        assert abs(delivered - iv.heat_delivered_kw) < 1e-6
        # Every kW of CHP heat is used, stored or dumped; none disappears.
        assert abs(
            iv.chp_heat_kw - (chp_heat_used + iv.buffer_charge_kw + iv.heat_dumped_kw)
        ) < 1e-6


def test_dispatch_does_not_clip_infeasible_requests(hub, conditions):
    """The checker must be able to see a violation, so dispatch must not repair it."""
    plan = flat_plan("d", heat_source="boiler", lighting_level=0.0,
                     battery="discharge", battery_power_kw=hub.battery.max_discharge_kw)
    result = dispatch_plan(plan, hub, list(conditions))
    assert result.intervals[-1].battery_soc_kwh < hub.battery.soc_min_kwh, (
        "dispatch silently clipped an over-discharge; the checker would then never "
        "see it and the headline measurement would read zero"
    )


def test_full_buffer_cannot_accept_more_heat():
    """A full tank is a physical limit, not a plan-feasibility question."""
    hub = EnergyHub(buffer=HeatBuffer(capacity_kwh=1000.0, level_init_frac=0.95,
                                      max_charge_kw=5000.0))
    conds = [HourlyConditions(hour=h, heat_demand_kw=0.0) for h in range(24)]
    plan = flat_plan("d", heat_source="boiler", chp_mode="max_export", lighting_level=0.0)
    result = dispatch_plan(plan, hub, conds)
    for iv in result.intervals:
        assert iv.buffer_level_kwh <= hub.buffer.level_max_kwh + 1e-6
        assert iv.heat_dumped_kw > 0, "surplus heat with a full buffer must be dumped"


def test_chp_ramp_is_enforced_in_dispatch():
    hub = EnergyHub(chp=dataclasses.replace(EnergyHub().chp, ramp_kw_per_hour=200.0))
    conds = [HourlyConditions(hour=h, heat_demand_kw=2000.0) for h in range(24)]
    plan = flat_plan("d", heat_source="chp", chp_mode="max_export", lighting_level=0.0)
    result = dispatch_plan(plan, hub, conds)
    previous = 0.0
    for iv in result.intervals:
        assert iv.chp_electrical_kw - previous <= 200.0 + 1e-6
        previous = iv.chp_electrical_kw


def test_battery_efficiency_costs_energy():
    """A charge/discharge round trip must lose energy, never create it."""
    hub = EnergyHub(battery=Battery(capacity_kwh=1000.0, soc_init_frac=0.5,
                                    charge_efficiency=0.9, discharge_efficiency=0.9,
                                    max_charge_kw=100.0, max_discharge_kw=100.0))
    conds = [HourlyConditions(hour=h) for h in range(24)]
    charge = flat_plan("d", heat_source="none", battery="charge", battery_power_kw=100.0)
    first = dispatch_plan(charge, hub, conds).intervals[0]
    stored = first.battery_soc_kwh - hub.battery.soc_init_kwh
    assert stored < 100.0


def test_congestion_window_limits():
    from kasflex.energy.assets import ContractLimits

    contract = ContractLimits(import_limit_kw=6000, export_limit_kw=4000,
                              congestion_windows={17: (1000.0, 0.0)})
    assert contract.limits_at(17) == (1000.0, 0.0)
    assert contract.limits_at(16) == (6000, 4000)


def test_hourly_dli_scales_with_lamp_level(hub):
    assert hub.hourly_dli_mol_m2(0.0) == 0.0
    assert abs(hub.hourly_dli_mol_m2(0.5) - hub.hourly_dli_mol_m2(1.0) / 2) < 1e-9
