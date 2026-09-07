"""The greenhouse physics seam.

KasFlex does not implement greenhouse physics. It defines the narrow interface it
needs -- :class:`GreenhouseModel` -- and ships two implementations:

* :class:`SurrogateGreenhouse`, dependency-free and deterministic, so the pipeline
  runs and the tests pass on a bare clone with no network. It is a crude
  first-order model and is **not validated against anything**; any run using it is
  stamped as such in its result record.
* :class:`~kasflex.adapters.greenlight_worker.GreenLightWorker`, which drives the
  real GreenLight-Gym2 model in a separate process and a separate virtual
  environment.

The interface is what makes that substitution safe, and it is deliberately small:
KasFlex asks for a day of climate and crop response given a day of energy intent,
and gets back demands, a climate projection, and the outcome figures the study
reports. Anything richer would couple KasFlex to one particular simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from kasflex.energy.dispatch import HourlyConditions
from kasflex.intent import Plan


@dataclass(frozen=True)
class DayOutcome:
    """What the greenhouse did over one simulated day."""

    heat_demand_kw: tuple[float, ...]
    co2_demand_kg_h: tuple[float, ...]
    temp_c: tuple[float, ...]
    rh_pct: tuple[float, ...]
    co2_ppm: tuple[float, ...]
    fruit_growth_kg_m2: float = 0.0
    natural_dli_mol_m2: float = 0.0
    model: str = "unknown"
    validated: bool = False
    """True only for a model whose deviation from measured data has been quantified
    and published (acceptance criterion 1). The surrogate is never validated."""
    diagnostics: dict[str, float] = field(default_factory=dict)

    def projection(self) -> dict[str, list[float]]:
        """The climate projection the checker's PROJECTED crop checks run against."""
        return {
            "temp_c": list(self.temp_c),
            "rh_pct": list(self.rh_pct),
            "co2_ppm": list(self.co2_ppm),
        }

    def temperature_band_hours(self, low: float, high: float) -> int:
        """Hours spent inside the crop temperature band (R5)."""
        return sum(1 for t in self.temp_c if low <= t <= high)


@runtime_checkable
class GreenhouseModel(Protocol):
    """Everything KasFlex needs from a greenhouse simulator."""

    name: str

    def simulate_day(
        self, plan: Plan, conditions: tuple[HourlyConditions, ...], floor_area_m2: float
    ) -> DayOutcome:
        """Simulate one day under ``plan`` and return the resulting demands and climate."""
        ...


@dataclass
class SurrogateGreenhouse:
    """A first-order greenhouse stand-in. Deterministic, fast, and not validated.

    A single lumped thermal capacity, a static ventilation and transpiration term,
    and a linear CO2 balance. It reproduces the *shape* a grower would recognise --
    heat demand rises at night and falls with sunlight, lamps add both light and
    heat, humidity climbs when the greenhouse is closed -- which is enough to
    exercise the planner, the checker and the interface end to end.

    It is not a physics claim. Stage 1 of the MVP plan replaces it with
    GreenLight-Gym2 and quantifies the difference; until then every result carries
    ``validated=False``.
    """

    name: str = "surrogate-v1"
    setpoint_day_c: float = 19.5
    setpoint_night_c: float = 16.5
    heat_loss_kw_per_m2_per_k: float = 0.0062
    """Overall heat transfer through the cover, per m2 of floor and per K of
    difference with outdoor air. A typical Dutch double-screened glasshouse."""
    thermal_mass_kwh_per_m2_per_k: float = 0.0085
    lamp_heat_fraction: float = 0.85
    """Share of lamp electrical input that ends up as sensible heat inside."""
    co2_uptake_kg_per_m2_per_hour_full_light: float = 0.0012
    vent_kw_per_m2_per_k: float = 0.150
    """Proportional ventilation gain: heat removed per K of overshoot above the
    setpoint, per m2 of floor. Sized so that open roof vents can reject a full
    summer solar load within a few K. Stands in for the roof-vent controller."""
    substeps_per_hour: int = 12
    """Integration sub-steps within each planning hour. Twelve gives a 5-minute
    step, comfortably inside the greenhouse thermal time constant."""

    def simulate_day(
        self, plan: Plan, conditions: tuple[HourlyConditions, ...], floor_area_m2: float
    ) -> DayOutcome:
        if len(conditions) != len(plan.intervals):
            raise ValueError("plan and conditions must cover the same hours")

        lamp_kw_per_m2 = 0.110  # matches EnergyHub.lamp_power_w_m2 default
        temp = self.setpoint_night_c
        heat: list[float] = []
        co2_demand: list[float] = []
        temps: list[float] = []
        rhs: list[float] = []
        co2s: list[float] = []
        natural_dli = 0.0

        for intent, cond in zip(plan.intervals, conditions, strict=True):
            is_day = 6 <= intent.hour < 20
            setpoint = self.setpoint_day_c if is_day else self.setpoint_night_c

            outdoor_c = cond.outdoor_temp_c

            loss_kw = (
                self.heat_loss_kw_per_m2_per_k
                * floor_area_m2
                * max(0.0, setpoint - outdoor_c)
            )
            solar_gain_kw = 0.55 * cond.irradiance_w_m2 * floor_area_m2 / 1000.0
            lamp_gain_kw = (
                intent.lighting_level * lamp_kw_per_m2 * floor_area_m2 * self.lamp_heat_fraction
            )
            demand_kw = max(0.0, loss_kw - solar_gain_kw - lamp_gain_kw)
            heat.append(demand_kw)

            # Realised temperature. The thermal time constant of a glasshouse is
            # well under an hour, so a single explicit Euler step per hour is
            # unstable under full sun; the hour is sub-stepped instead. Ventilation
            # is proportional to the overshoot above setpoint and is what rejects
            # the solar load on a bright day.
            supplied_kw = demand_kw if intent.heat_source != "none" else 0.0
            capacity = self.thermal_mass_kwh_per_m2_per_k * floor_area_m2
            dt_h = 1.0 / self.substeps_per_hour
            for _ in range(self.substeps_per_hour):
                vent_kw = (
                    max(0.0, temp - setpoint) * self.vent_kw_per_m2_per_k * floor_area_m2
                )
                loss_now_kw = self.heat_loss_kw_per_m2_per_k * floor_area_m2 * (temp - outdoor_c)
                net_kw = supplied_kw + solar_gain_kw + lamp_gain_kw - loss_now_kw - vent_kw
                temp = temp + net_kw * dt_h / capacity if capacity > 0 else temp
                temp = max(-5.0, min(45.0, temp))
            temps.append(temp)

            # Humidity rises with transpiration (light-driven) and falls with heating.
            rh = (
                72.0
                + 14.0 * (cond.irradiance_w_m2 / 500.0)
                - 0.9 * (supplied_kw / max(1.0, loss_kw)) * 6.0
            )
            rhs.append(max(35.0, min(99.0, rh)))

            light_frac = min(1.0, cond.irradiance_w_m2 / 400.0 + intent.lighting_level)
            uptake = self.co2_uptake_kg_per_m2_per_hour_full_light * floor_area_m2 * light_frac
            co2_demand.append(uptake)
            enriched = intent.co2_source != "none"
            co2s.append(900.0 if enriched and light_frac > 0.05 else 400.0 - 60.0 * light_frac)

            # 1 W/m2 of global radiation is roughly 2.1 umol/m2/s PAR at the canopy.
            natural_dli += cond.irradiance_w_m2 * 2.1 * 0.45 * 3600.0 / 1e6

        # Growth proxy: light-limited, penalised outside the temperature band.
        total_light = natural_dli + sum(
            0.185 * iv.lighting_level * 3600.0 / 1e6 * 1000.0 for iv in plan.intervals
        )
        in_band = sum(1 for t in temps if 15.0 <= t <= 34.0) / max(1, len(temps))
        growth = 0.0016 * total_light * in_band

        return DayOutcome(
            heat_demand_kw=tuple(heat),
            co2_demand_kg_h=tuple(co2_demand),
            temp_c=tuple(temps),
            rh_pct=tuple(rhs),
            co2_ppm=tuple(co2s),
            fruit_growth_kg_m2=growth,
            natural_dli_mol_m2=natural_dli,
            model=self.name,
            validated=False,
            diagnostics={"mean_heat_kw": sum(heat) / len(heat) if heat else 0.0},
        )
