"""Intent-level rule-based baseline (R7).

GreenLight-Gym2 ships a rule-based controller, but it works at actuator level. If
the baseline planned actuators while the language model planned intent, the
experiment would measure the low-level controller rather than the planner. So the
baseline is lifted to intent level here, and the GL-Gym rule base stays where it
belongs: inside the worker, translating accepted intent into setpoints.

The rules are the ones a Dutch grower would recognise: light when power is cheap
and the crop still needs light, run the CHP when the spark spread is positive,
charge the battery overnight and discharge into the evening peak, and lean on the
heat buffer when power and gas are both expensive.
"""

from __future__ import annotations

from dataclasses import dataclass

from kasflex.controllers.base import PlanningContext
from kasflex.intent import HOURS_PER_DAY, IntervalIntent, Plan


@dataclass
class RuleBasedPlanner:
    """A deterministic, defensible baseline. No learning, no search, no model.

    Attributes:
        spark_spread_margin: Minimum margin, in EUR/kWh, by which the power price
            must exceed the CHP's marginal generation cost before the unit is run.
        battery_cycle_hours: How many hours to charge and to discharge per day.
        lamp_curfew_hours: Hours in which lighting is never scheduled, standing in
            for light-emission rules and the GL-Gym lamp penalty after 20:00.
        min_buffer_reserve_frac: Fraction of buffer capacity the baseline refuses
            to plan below, leaving headroom for forecast error.
        contract_headroom_frac: Fraction of the available electrical headroom the
            baseline is willing to fill with lighting, leaving the rest as margin
            against forecast error.
    """

    name: str = "rule-based"
    spark_spread_margin: float = 0.01
    battery_cycle_hours: int = 4
    lamp_curfew_hours: tuple[int, ...] = (20, 21, 22, 23)
    min_buffer_reserve_frac: float = 0.25
    contract_headroom_frac: float = 0.95

    def plan(self, context: PlanningContext) -> Plan:
        hub = context.hub
        forecast = context.forecast

        # --- CHP: run when the spark spread justifies it -----------------------
        chp = hub.chp
        run_chp: dict[int, bool] = {}
        for c in forecast:
            marginal_cost = (
                c.gas_price_eur_kwh / chp.electrical_efficiency
                if chp.electrical_efficiency > 0
                else float("inf")
            )
            run_chp[c.hour] = c.power_price_eur_kwh > marginal_cost + self.spark_spread_margin

        # A CHP feeding a congested feeder is the problem, not the solution: the
        # export limit binds hardest in exactly the evening hours where the spark
        # spread is widest. Block full export wherever it would breach the
        # contract, before committing the unit.
        for c in forecast:
            if not run_chp[c.hour]:
                continue
            _, export_limit = hub.contract.limits_at(c.hour)
            pv_kw = hub.pv.generation_kw(c.irradiance_w_m2)
            # Worst case for export is an hour with no lighting load, which is
            # precisely when the baseline wants to run the unit.
            projected_export = chp.electrical_capacity_kw + pv_kw - hub.base_load_kw
            if projected_export > export_limit:
                run_chp[c.hour] = False

        # Respect the minimum run and down times, so the baseline never produces a
        # plan the checker would reject. A baseline that fails verification would
        # make the fallback path (R18) meaningless.
        run_chp = _enforce_min_durations(run_chp, chp.min_run_hours, chp.min_down_hours)

        # --- battery: charge cheap, discharge dear -----------------------------
        n = self.battery_cycle_hours
        cheap = set(context.cheapest_hours(n))
        dear = set(context.dearest_hours(n)) - cheap
        # Size the cycle so the state of charge stays inside its band all day.
        usable = hub.battery.soc_max_kwh - hub.battery.soc_init_kwh
        drawable = hub.battery.soc_init_kwh - hub.battery.soc_min_kwh
        charge_kw = min(
            hub.battery.max_charge_kw,
            hub.battery.c_rate_power_kw,
            0.98 * usable / max(1, n) / max(1e-6, hub.battery.charge_efficiency),
        )
        discharge_kw = min(
            hub.battery.max_discharge_kw,
            hub.battery.c_rate_power_kw,
            0.98 * drawable / max(1, n) * hub.battery.discharge_efficiency,
        )

        # --- lighting: fill the DLI target from the cheapest permitted hours ---
        # Scheduled last, and capped by the electrical headroom left once the base
        # load, the battery and the CHP are accounted for. A 5 ha lamp field is
        # several megawatts, so lighting is what actually decides whether the
        # connection contract holds; planning it blind to the contract would make
        # the baseline fail verification in almost every hour.
        per_hour_dli = hub.hourly_dli_mol_m2(1.0)
        lighting = dict.fromkeys(range(HOURS_PER_DAY), 0.0)
        remaining_dli = hub.crop.dli_target_mol_m2
        eligible = [
            c
            for c in sorted(forecast, key=lambda c: c.power_price_eur_kwh)
            if c.hour not in self.lamp_curfew_hours
        ]
        for c in eligible:
            if remaining_dli <= 1e-9 or per_hour_dli <= 0:
                break
            import_limit, _ = hub.contract.limits_at(c.hour)
            charging = charge_kw if c.hour in cheap else 0.0
            supply = hub.pv.generation_kw(c.irradiance_w_m2)
            supply += chp.electrical_capacity_kw if run_chp[c.hour] else 0.0
            supply += discharge_kw if c.hour in dear else 0.0
            headroom_kw = import_limit + supply - hub.base_load_kw - charging
            # Leave a small margin: the plan is made against a forecast, and the
            # realised base load will not match it exactly.
            headroom_kw *= self.contract_headroom_frac
            cap = max(0.0, min(1.0, headroom_kw / hub.lamp_capacity_kw))
            level = min(cap, remaining_dli / per_hour_dli)
            lighting[c.hour] = level
            remaining_dli -= level * per_hour_dli

        # --- assemble ----------------------------------------------------------
        intervals = []
        buffer_reserve = hub.buffer.capacity_kwh * self.min_buffer_reserve_frac
        buffer_level = hub.buffer.level_init_kwh

        for c in forecast:
            hour = c.hour
            chp_on = run_chp[hour]

            # Prefer stored heat in the dearest hours, provided the reserve holds.
            use_buffer = (
                hour in dear
                and not chp_on
                and buffer_level - c.heat_demand_kw > buffer_reserve
                and c.heat_demand_kw <= hub.buffer.max_discharge_kw
            )
            if chp_on:
                heat_source = "chp"
            elif use_buffer:
                heat_source = "buffer"
                buffer_level -= c.heat_demand_kw
            else:
                heat_source = "boiler"

            if hour in cheap:
                battery, power = "charge", charge_kw
            elif hour in dear:
                battery, power = "discharge", discharge_kw
            else:
                battery, power = "idle", 0.0

            daylight = c.irradiance_w_m2 > 20.0 or lighting[hour] > 0.05
            co2_source = "chp" if (chp_on and daylight) else ("liquid" if daylight else "none")

            intervals.append(
                IntervalIntent(
                    hour=hour,
                    heat_source=heat_source,
                    lighting_level=round(lighting[hour], 4),
                    battery=battery,
                    battery_power_kw=round(power, 2),
                    chp_mode="max_export" if chp_on else "off",
                    co2_source=co2_source,
                    reasoning=(
                        f"price {c.power_price_eur_kwh:.3f} EUR/kWh; "
                        f"heat from {heat_source}; "
                        f"CHP {'on' if chp_on else 'off'}; battery {battery}"
                    ),
                )
            )

        return Plan(
            date=context.date,
            intervals=tuple(intervals),
            planner=self.name,
            brief=context.brief,
            revision=context.revision,
            notes="Deterministic intent-level baseline.",
        )


def _enforce_min_durations(
    schedule: dict[int, bool], min_run: int, min_down: int
) -> dict[int, bool]:
    """Smooth a raw on/off schedule so every run and every gap is long enough.

    The smoothing only ever turns the unit **off**. A run shorter than the minimum
    run time is removed; a gap shorter than the minimum down time is widened by
    removing the run that follows it. Filling a short gap instead -- turning the
    unit on to bridge it -- would be the other valid repair, but it can switch on
    an hour that was ruled out for exceeding the export limit, and would then
    reintroduce the violation this function is meant to keep the baseline clear of.

    Turning off is also the conservative direction economically: it can only cost
    the baseline some arbitrage revenue, never create an infeasible commitment.
    Because every pass strictly reduces the number of running hours, the loop
    terminates.
    """
    hours = sorted(schedule)
    state = [schedule[h] for h in hours]
    min_run = max(1, min_run)
    min_down = max(1, min_down)

    changed = True
    while changed:
        changed = False

        # Remove runs that are too short.
        i = 0
        while i < len(state):
            if not state[i]:
                i += 1
                continue
            j = i
            while j < len(state) and state[j]:
                j += 1
            if j - i < min_run:
                for k in range(i, j):
                    state[k] = False
                changed = True
            i = j

        # Widen gaps that are too short, by removing the run after them.
        i = 0
        while i < len(state):
            if state[i]:
                i += 1
                continue
            j = i
            while j < len(state) and not state[j]:
                j += 1
            if j - i < min_down and j < len(state):
                k = j
                while k < len(state) and state[k]:
                    state[k] = False
                    k += 1
                changed = True
                i = k
            else:
                i = j

    return {h: state[i] for i, h in enumerate(hours)}
