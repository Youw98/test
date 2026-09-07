"""Deterministic dispatch: hourly energy intent to asset flows.

This is the energy-hub half of the low-level controller (R10). Given a
:class:`~kasflex.intent.Plan` and a day of demands and prices, it produces exactly
one :class:`DispatchResult` -- same input, same output, every time, no solver and
no randomness.

One design decision matters more than any other in this module:

    **Dispatch never silently clips an infeasible request.**

If a plan asks the battery to discharge past its floor, or asks the grid for more
than the contract allows, dispatch carries out the request as written and records
the resulting out-of-bounds state. Clipping here would quietly repair bad plans and
the checker would have nothing left to catch -- which would destroy the very
measurement KasFlex exists to make (violations with the checker disabled versus
enabled). Feasibility is the checker's job, not dispatch's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kasflex.energy.assets import EnergyHub
from kasflex.intent import IntervalIntent, Plan


@dataclass(frozen=True)
class HourlyConditions:
    """Everything outside the hub that the dispatch needs for one hour.

    Demands are absolute (kW for the whole greenhouse), already scaled from the
    greenhouse model's per-square-metre output by the hub floor area.
    """

    hour: int
    heat_demand_kw: float = 0.0
    co2_demand_kg_h: float = 0.0
    irradiance_w_m2: float = 0.0
    outdoor_temp_c: float = 5.0
    """Outdoor air temperature. The dominant driver of greenhouse heat demand, and
    therefore the single most useful feature a demand forecaster has. Carried here
    so that forecasting and physics read it from the same place."""
    power_price_eur_kwh: float = 0.10
    gas_price_eur_kwh: float = 0.035
    feed_in_price_eur_kwh: float | None = None
    """Export price. Defaults to the power price when None."""

    @property
    def export_price(self) -> float:
        if self.feed_in_price_eur_kwh is None:
            return self.power_price_eur_kwh
        return self.feed_in_price_eur_kwh


@dataclass(frozen=True)
class IntervalDispatch:
    """Realised flows and state for one hour. All power values in kW, energy in kWh."""

    hour: int
    lighting_kw: float
    base_load_kw: float
    pv_kw: float
    chp_electrical_kw: float
    chp_heat_kw: float
    boiler_heat_kw: float
    buffer_charge_kw: float
    buffer_discharge_kw: float
    buffer_level_kwh: float
    battery_charge_kw: float
    battery_discharge_kw: float
    battery_soc_kwh: float
    grid_import_kw: float
    grid_export_kw: float
    gas_input_kw: float
    co2_from_chp_kg: float
    co2_liquid_kg: float
    heat_demand_kw: float
    heat_delivered_kw: float
    heat_dumped_kw: float
    dli_contribution_mol_m2: float
    chp_running: bool
    energy_cost_eur: float
    """Net cost for the hour: gas plus grid import, minus export revenue, plus liquid CO2."""

    @property
    def grid_net_kw(self) -> float:
        """Positive when importing, negative when exporting."""
        return self.grid_import_kw - self.grid_export_kw

    @property
    def heat_shortfall_kw(self) -> float:
        return max(0.0, self.heat_demand_kw - self.heat_delivered_kw)


@dataclass(frozen=True)
class DispatchResult:
    """The full day: 24 hourly dispatches plus the totals the study reports (R5)."""

    intervals: tuple[IntervalDispatch, ...]
    hub: EnergyHub
    chp_run_state: tuple[bool, ...] = field(default_factory=tuple)

    @property
    def total_cost_eur(self) -> float:
        return sum(iv.energy_cost_eur for iv in self.intervals)

    @property
    def gas_cost_eur(self) -> float:
        # Recomputed from flows so the split always reconciles with the total.
        return sum(iv.gas_input_kw for iv in self.intervals) * self._mean_gas_price

    @property
    def total_dli_mol_m2(self) -> float:
        return sum(iv.dli_contribution_mol_m2 for iv in self.intervals)

    @property
    def peak_import_kw(self) -> float:
        return max((iv.grid_import_kw for iv in self.intervals), default=0.0)

    @property
    def peak_export_kw(self) -> float:
        return max((iv.grid_export_kw for iv in self.intervals), default=0.0)

    @property
    def total_heat_shortfall_kwh(self) -> float:
        return sum(iv.heat_shortfall_kw for iv in self.intervals)

    _mean_gas_price: float = 0.035

    def summary(self) -> dict[str, float]:
        """Per-day reporting figures (R5), including per-square-metre scaling (ADR-0004)."""
        area = self.hub.floor_area_m2
        return {
            "net_cost_eur": round(self.total_cost_eur, 2),
            "net_cost_eur_per_m2": round(self.total_cost_eur / area, 5),
            "peak_import_kw": round(self.peak_import_kw, 2),
            "peak_export_kw": round(self.peak_export_kw, 2),
            "grid_import_kwh": round(sum(iv.grid_import_kw for iv in self.intervals), 2),
            "grid_export_kwh": round(sum(iv.grid_export_kw for iv in self.intervals), 2),
            "gas_input_kwh": round(sum(iv.gas_input_kw for iv in self.intervals), 2),
            "dli_mol_m2": round(self.total_dli_mol_m2, 3),
            "heat_shortfall_kwh": round(self.total_heat_shortfall_kwh, 2),
            "final_soc_kwh": round(self.intervals[-1].battery_soc_kwh, 2),
            "final_buffer_kwh": round(self.intervals[-1].buffer_level_kwh, 2),
        }


def _chp_setpoint_kw(hub: EnergyHub, mode: str, heat_demand_kw: float, previous_kw: float) -> float:
    """Electrical setpoint implied by a CHP mode, after applying the ramp limit."""
    chp = hub.chp
    if mode == "off":
        target = 0.0
    elif mode == "max_export":
        target = chp.electrical_capacity_kw
    elif mode == "heat_led":
        # Follow the heat demand, but never run below the technical minimum load.
        implied = heat_demand_kw / chp.heat_to_power_ratio if chp.heat_to_power_ratio > 0 else 0.0
        if implied <= 0.0:
            target = 0.0
        else:
            target = min(chp.electrical_capacity_kw, max(chp.min_load_kw, implied))
    else:  # pragma: no cover - guarded by the intent schema
        raise ValueError(f"unknown chp_mode {mode!r}")

    # The ramp limit is physical and always applies; it is not a feasibility check.
    # Min run/down time, by contrast, IS checked rather than enforced, so that a
    # plan violating it remains visible to the checker.
    delta = target - previous_kw
    if delta > chp.ramp_kw_per_hour:
        target = previous_kw + chp.ramp_kw_per_hour
    elif delta < -chp.ramp_kw_per_hour:
        target = previous_kw - chp.ramp_kw_per_hour
    return max(0.0, target)


@dataclass(frozen=True)
class HubState:
    """Carried state between hours: what the storage holds and what the CHP is doing."""

    battery_soc_kwh: float
    buffer_level_kwh: float
    prev_chp_electrical_kw: float

    @classmethod
    def initial(cls, hub: EnergyHub) -> HubState:
        return cls(
            battery_soc_kwh=hub.battery.soc_init_kwh,
            buffer_level_kwh=hub.buffer.level_init_kwh,
            prev_chp_electrical_kw=(
                hub.chp.electrical_capacity_kw if hub.chp.initially_running else 0.0
            ),
        )


def dispatch_hour(
    intent: IntervalIntent,
    hub: EnergyHub,
    cond: HourlyConditions,
    state: HubState,
) -> tuple[IntervalDispatch, HubState]:
    """Dispatch a single hour. Pure: same inputs, same outputs, no side effects.

    Extracted so that a scheduler searching over candidate plans scores them against
    exactly the model :func:`dispatch_plan` runs and the checker inspects. A
    scheduler optimising its own approximation of the hub would produce plans that
    look optimal and then fail verification, which is the most tedious class of bug
    this project could have.

    Returns:
        The hour's realised flows, and the state to carry into the next hour.
    """
    soc = state.battery_soc_kwh
    buffer_level = state.buffer_level_kwh
    prev_chp_kw = state.prev_chp_electrical_kw

    lighting_kw = hub.lighting_kw(intent.lighting_level)
    pv_kw = hub.pv.generation_kw(cond.irradiance_w_m2)

    # --- Heat side -------------------------------------------------------
    chp_e_kw = _chp_setpoint_kw(hub, intent.chp_mode, cond.heat_demand_kw, prev_chp_kw)
    chp_heat_kw = chp_e_kw * hub.chp.heat_to_power_ratio
    heat_demand = max(0.0, cond.heat_demand_kw)

    boiler_kw = 0.0
    buffer_discharge_kw = 0.0
    heat_from_chp_kw = 0.0

    if intent.heat_source == "none":
        remaining = 0.0
    elif intent.heat_source == "chp":
        heat_from_chp_kw = min(chp_heat_kw, heat_demand)
        remaining = heat_demand - heat_from_chp_kw
        boiler_kw = min(hub.boiler.thermal_capacity_kw, remaining)
        remaining -= boiler_kw
    elif intent.heat_source == "buffer":
        # Requested as asked, up to the buffer's power rating. Whether the
        # resulting level is legal is the checker's call.
        buffer_discharge_kw = min(hub.buffer.max_discharge_kw, heat_demand)
        remaining = heat_demand - buffer_discharge_kw
        boiler_kw = min(hub.boiler.thermal_capacity_kw, remaining)
        remaining -= boiler_kw
    else:  # "boiler"
        boiler_kw = min(hub.boiler.thermal_capacity_kw, heat_demand)
        remaining = heat_demand - boiler_kw

    heat_delivered = heat_from_chp_kw + boiler_kw + buffer_discharge_kw

    # CHP heat the greenhouse did not take is stored, and dumped if it will not
    # fit. Both the charge *rate* and the remaining *volume* bind here: a full
    # tank cannot accept heat however fast you push it. This is a physical
    # limit, not a feasibility judgement -- the plan never asked to overfill the
    # buffer, the surplus is a consequence of the CHP setpoint -- so capping it
    # here does not hide anything the checker should have caught. Heat that
    # cannot be stored is dumped to the ambient and reported, because running a
    # CHP to dump its heat is exactly the kind of waste an operator wants to see.
    after_losses = buffer_level * (1.0 - hub.buffer.standing_loss_frac_per_hour)
    surplus_chp_heat = max(0.0, chp_heat_kw - heat_from_chp_kw)
    headroom_kwh = max(0.0, hub.buffer.level_max_kwh - after_losses + buffer_discharge_kw)
    buffer_charge_kw = min(surplus_chp_heat, hub.buffer.max_charge_kw, headroom_kwh)
    heat_dumped_kw = surplus_chp_heat - buffer_charge_kw

    buffer_level = after_losses + buffer_charge_kw - buffer_discharge_kw

    # --- Electrical side -------------------------------------------------
    battery_charge_kw = intent.battery_power_kw if intent.battery == "charge" else 0.0
    battery_discharge_kw = intent.battery_power_kw if intent.battery == "discharge" else 0.0
    soc = (
        soc
        + battery_charge_kw * hub.battery.charge_efficiency
        - (battery_discharge_kw / hub.battery.discharge_efficiency
           if hub.battery.discharge_efficiency > 0 else 0.0)
    )

    elec_demand_kw = hub.base_load_kw + lighting_kw + battery_charge_kw
    elec_supply_kw = pv_kw + chp_e_kw + battery_discharge_kw
    net_kw = elec_demand_kw - elec_supply_kw
    grid_import_kw = max(0.0, net_kw)
    grid_export_kw = max(0.0, -net_kw)

    # --- CO2 -------------------------------------------------------------
    co2_available_from_chp = chp_e_kw * hub.chp.co2_kg_per_kwh_e
    if intent.co2_source == "chp":
        co2_from_chp = min(co2_available_from_chp, cond.co2_demand_kg_h)
        co2_liquid = 0.0
    elif intent.co2_source == "liquid":
        co2_from_chp = 0.0
        co2_liquid = cond.co2_demand_kg_h
    else:
        co2_from_chp = 0.0
        co2_liquid = 0.0

    # --- Costs -----------------------------------------------------------
    gas_input_kw = hub.boiler.gas_input_kw(boiler_kw) + hub.chp.gas_input_kw(chp_e_kw)
    cost = (
        gas_input_kw * cond.gas_price_eur_kwh
        + grid_import_kw * cond.power_price_eur_kwh
        - grid_export_kw * cond.export_price
        + co2_liquid * 0.30  # liquid CO2, EUR/kg
    )

    interval = IntervalDispatch(
        hour=intent.hour,
        lighting_kw=lighting_kw,
        base_load_kw=hub.base_load_kw,
        pv_kw=pv_kw,
        chp_electrical_kw=chp_e_kw,
        chp_heat_kw=chp_heat_kw,
        boiler_heat_kw=boiler_kw,
        buffer_charge_kw=buffer_charge_kw,
        buffer_discharge_kw=buffer_discharge_kw,
        buffer_level_kwh=buffer_level,
        battery_charge_kw=battery_charge_kw,
        battery_discharge_kw=battery_discharge_kw,
        battery_soc_kwh=soc,
        grid_import_kw=grid_import_kw,
        grid_export_kw=grid_export_kw,
        gas_input_kw=gas_input_kw,
        co2_from_chp_kg=co2_from_chp,
        co2_liquid_kg=co2_liquid,
        heat_demand_kw=heat_demand,
        heat_delivered_kw=heat_delivered,
        heat_dumped_kw=heat_dumped_kw,
        dli_contribution_mol_m2=hub.hourly_dli_mol_m2(intent.lighting_level),
        chp_running=chp_e_kw > 0.0,
        energy_cost_eur=cost,
    )

    return interval, HubState(
        battery_soc_kwh=soc,
        buffer_level_kwh=buffer_level,
        prev_chp_electrical_kw=chp_e_kw,
    )


def dispatch_plan(
    plan: Plan,
    hub: EnergyHub,
    conditions: list[HourlyConditions],
) -> DispatchResult:
    """Run ``plan`` against ``hub`` under ``conditions`` and return the realised flows.

    Args:
        plan: 24 hours of energy intent.
        hub: The energy hub, including all asset limits and initial states.
        conditions: 24 hourly condition records, ordered by hour.

    Returns:
        A :class:`DispatchResult` whose intervals may contain out-of-bounds states.
        Feasibility is decided by :mod:`kasflex.checker`, not here.

    Raises:
        ValueError: if ``conditions`` does not supply exactly one record per hour.
    """
    if len(conditions) != len(plan.intervals):
        raise ValueError(
            f"expected {len(plan.intervals)} hourly conditions, got {len(conditions)}"
        )

    state = HubState.initial(hub)

    intervals: list[IntervalDispatch] = []
    run_state: list[bool] = []
    gas_prices: list[float] = []

    for intent, cond in zip(plan.intervals, conditions, strict=True):
        gas_prices.append(cond.gas_price_eur_kwh)
        interval, state = dispatch_hour(intent, hub, cond, state)
        intervals.append(interval)
        run_state.append(interval.chp_running)

    mean_gas = sum(gas_prices) / len(gas_prices) if gas_prices else 0.035
    return DispatchResult(
        intervals=tuple(intervals),
        hub=hub,
        chp_run_state=tuple(run_state),
        _mean_gas_price=mean_gas,
    )
