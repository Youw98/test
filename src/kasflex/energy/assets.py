"""Energy-hub asset models and the limits the safety checker enforces.

Everything here is a plain dataclass with explicit units. There is no hidden state:
an asset describes *what is possible*, and :mod:`kasflex.energy.dispatch` computes
*what actually happens* for a given plan. Keeping the two apart is what lets the
checker replay a dispatch deterministically before anything is executed.

Unit convention throughout KasFlex:
  * power        kW      (electrical and thermal, disambiguated by name)
  * energy       kWh
  * prices       EUR/kWh
  * gas          kWh of higher heating value, priced in EUR/kWh
  * CO2          kg
  * areas        m^2
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContractLimits:
    """Grid connection contract (R14).

    Attributes:
        import_limit_kw: Maximum sustained offtake from the grid.
        export_limit_kw: Maximum sustained feed-in. Often smaller than the import
            limit for a horticultural connection, and sometimes zero under a
            non-firm connection agreement.
        congestion_windows: Hours of the day during which a reduced limit applies,
            as ``{hour: (import_kw, export_kw)}``. This is how a DSO congestion
            instruction or a non-firm ATO profile enters the model.

    The defaults describe a roughly 5 ha lit greenhouse: enough capacity to run the
    lamp field, which is what makes a congestion window bite rather than being
    academic. A connection smaller than the installed lamp load would make every
    fully lit hour a violation regardless of what the planner did.
    """

    import_limit_kw: float = 6000.0
    export_limit_kw: float = 4000.0
    congestion_windows: dict[int, tuple[float, float]] = field(default_factory=dict)

    def limits_at(self, hour: int) -> tuple[float, float]:
        """Return ``(import_kw, export_kw)`` in force during ``hour``."""
        return self.congestion_windows.get(hour, (self.import_limit_kw, self.export_limit_kw))


@dataclass(frozen=True)
class Battery:
    """Electrical storage.

    ``c_rate_max`` bounds power as a multiple of capacity, independently of
    ``max_charge_kw``/``max_discharge_kw``; the binding limit is the smaller of the
    two, and the checker reports whichever one was actually violated.
    """

    capacity_kwh: float = 2000.0
    max_charge_kw: float = 1000.0
    max_discharge_kw: float = 1000.0
    soc_min_frac: float = 0.10
    soc_max_frac: float = 0.90
    soc_init_frac: float = 0.50
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    c_rate_max: float = 0.5

    @property
    def soc_min_kwh(self) -> float:
        return self.capacity_kwh * self.soc_min_frac

    @property
    def soc_max_kwh(self) -> float:
        return self.capacity_kwh * self.soc_max_frac

    @property
    def soc_init_kwh(self) -> float:
        return self.capacity_kwh * self.soc_init_frac

    @property
    def c_rate_power_kw(self) -> float:
        return self.capacity_kwh * self.c_rate_max


@dataclass(frozen=True)
class Chp:
    """Combined heat and power unit.

    A greenhouse CHP is three things at once: an electricity generator, a heat
    source and a CO2 source. ``co2_kg_per_kwh_e`` captures the flue-gas CO2 that
    would otherwise have to be bought as liquid CO2, which is why running the CHP
    can be worth it even when the power price alone does not justify it.
    """

    electrical_capacity_kw: float = 1500.0
    heat_to_power_ratio: float = 1.2
    electrical_efficiency: float = 0.40
    min_load_frac: float = 0.50
    min_run_hours: int = 2
    min_down_hours: int = 2
    ramp_kw_per_hour: float = 1500.0
    """A greenhouse gas engine reaches full load within minutes, so over a
    one-hour planning interval it can traverse its whole range. The limit is kept
    explicit because larger units and steam turbines cannot."""
    co2_kg_per_kwh_e: float = 0.45
    initially_running: bool = False
    hours_in_current_state: int = 99

    @property
    def thermal_capacity_kw(self) -> float:
        return self.electrical_capacity_kw * self.heat_to_power_ratio

    @property
    def min_load_kw(self) -> float:
        return self.electrical_capacity_kw * self.min_load_frac

    def gas_input_kw(self, electrical_kw: float) -> float:
        """Gas (HHV) drawn to produce ``electrical_kw`` of electricity."""
        if electrical_kw <= 0.0:
            return 0.0
        return electrical_kw / self.electrical_efficiency


@dataclass(frozen=True)
class Boiler:
    """Gas boiler. Heat only, fast, and the fallback whenever nothing else fits."""

    thermal_capacity_kw: float = 8000.0
    """Sized for the default 5 ha greenhouse. GreenLight-Gym2 reports a maximum
    heating power of 130 W/m2, so a 5 ha site needs about 6.5 MW on the coldest
    hour; 4 MW would leave the checker reporting an unmeetable heat demand on any
    genuinely cold night."""
    efficiency: float = 0.90
    ramp_kw_per_hour: float = 8000.0

    def gas_input_kw(self, thermal_kw: float) -> float:
        if thermal_kw <= 0.0:
            return 0.0
        return thermal_kw / self.efficiency


@dataclass(frozen=True)
class HeatBuffer:
    """Stratified hot-water buffer: the cheapest flexibility a greenhouse owns."""

    capacity_kwh: float = 8000.0
    max_charge_kw: float = 3000.0
    max_discharge_kw: float = 3000.0
    level_min_frac: float = 0.05
    level_max_frac: float = 0.95
    level_init_frac: float = 0.50
    standing_loss_frac_per_hour: float = 0.005

    @property
    def level_min_kwh(self) -> float:
        return self.capacity_kwh * self.level_min_frac

    @property
    def level_max_kwh(self) -> float:
        return self.capacity_kwh * self.level_max_frac

    @property
    def level_init_kwh(self) -> float:
        return self.capacity_kwh * self.level_init_frac


@dataclass(frozen=True)
class Pv:
    """Rooftop or field PV. Generation is derived from the forecast irradiance."""

    peak_kw: float = 500.0
    performance_ratio: float = 0.85

    def generation_kw(self, irradiance_w_m2: float) -> float:
        """Generation at a given plane-of-array irradiance, using the 1000 W/m^2 STC ratio."""
        return max(0.0, self.peak_kw * self.performance_ratio * irradiance_w_m2 / 1000.0)


@dataclass(frozen=True)
class CropLimits:
    """Crop-side bounds the checker enforces on a plan (R16).

    Only ``dli`` is exactly decidable before execution, because it follows from the
    lighting plan alone. The climate bands are carried here so the checker can test
    them against the projection returned by the greenhouse adapter, and so the same
    numbers are reported for both.
    """

    dli_target_mol_m2: float = 10.0
    dli_tolerance_mol_m2: float = 3.0
    """Target is for *supplemental* light only, not total light including sunlight.
    A 185 umol/m2/s lamp field delivers 0.67 mol/m2 per lit hour, so a supplemental
    target much above 12 mol/m2 cannot be met inside a normal lighting window at
    all, and would reject every plan for a reason no planner could act on."""
    temp_min_c: float = 15.0
    temp_max_c: float = 34.0
    rh_max_pct: float = 85.0
    co2_min_ppm: float = 300.0
    co2_max_ppm: float = 1600.0


@dataclass(frozen=True)
class EnergyHub:
    """The complete energy hub attached to one greenhouse.

    Attributes:
        floor_area_m2: Greenhouse floor area. Every per-square-metre figure coming
            out of the greenhouse model is scaled by this. The AGC research
            compartment is 96 m^2; a commercial scenario is around 50 000 m^2, a
            factor of roughly 500 -- see docs/DECISIONS.md ADR-0004.
        lamp_power_w_m2: Installed supplemental lighting power density.
        lamp_ppfd_umol_m2_s: Photosynthetic photon flux at full lamp power, used to
            turn the lighting plan into a daily light integral.
        base_load_kw: Site electrical load that is not lighting (pumps, fans,
            screens, packing hall).
    """

    floor_area_m2: float = 50_000.0
    lamp_power_w_m2: float = 110.0
    lamp_ppfd_umol_m2_s: float = 185.0
    base_load_kw: float = 150.0
    contract: ContractLimits = field(default_factory=ContractLimits)
    battery: Battery = field(default_factory=Battery)
    chp: Chp = field(default_factory=Chp)
    boiler: Boiler = field(default_factory=Boiler)
    buffer: HeatBuffer = field(default_factory=HeatBuffer)
    pv: Pv = field(default_factory=Pv)
    crop: CropLimits = field(default_factory=CropLimits)

    @property
    def lamp_capacity_kw(self) -> float:
        """Electrical draw with all lamps at full power."""
        return self.lamp_power_w_m2 * self.floor_area_m2 / 1000.0

    def lighting_kw(self, level: float) -> float:
        return self.lamp_capacity_kw * max(0.0, min(1.0, level))

    def hourly_dli_mol_m2(self, level: float) -> float:
        """Daily-light-integral contribution of one hour of lighting at ``level``.

        umol/m2/s over 3600 s, converted to mol/m2.
        """
        return self.lamp_ppfd_umol_m2_s * max(0.0, min(1.0, level)) * 3600.0 / 1e6
