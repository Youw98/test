"""Hourly energy intent: the contract between the planner and everything downstream.

The planner (rule-based, MPC or language model) never touches actuators. It emits
one :class:`IntervalIntent` per hour of the plan day. A deterministic low-level
controller turns intent into actuator setpoints at the simulator timestep, and the
safety checker verifies intent *before* any of that happens.

Keeping the planner at intent level is what makes the comparison in the study fair:
the rule-based baseline and the MPC reference emit the same structure, so the
experiment measures the planner and not the low-level controller (KasFlex R7-R10).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

HOURS_PER_DAY = 24

HeatSource = Literal["boiler", "chp", "buffer", "none"]
BatteryAction = Literal["charge", "discharge", "idle"]
ChpMode = Literal["off", "heat_led", "max_export"]
Co2Source = Literal["chp", "liquid", "none"]

HEAT_SOURCES: tuple[str, ...] = ("boiler", "chp", "buffer", "none")
BATTERY_ACTIONS: tuple[str, ...] = ("charge", "discharge", "idle")
CHP_MODES: tuple[str, ...] = ("off", "heat_led", "max_export")
CO2_SOURCES: tuple[str, ...] = ("chp", "liquid", "none")


class IntentSchemaError(ValueError):
    """Raised when a plan cannot be parsed into a well-formed :class:`Plan`.

    This is deliberately distinct from a checker rejection. A schema error means the
    planner produced something that is not a plan at all; a checker rejection means
    it produced a well-formed plan that violates a limit.
    """


@dataclass(frozen=True)
class IntervalIntent:
    """Energy intent for a single hour.

    Attributes:
        hour: Hour index within the plan day, 0-23.
        heat_source: Which asset is asked to cover the hour's heat demand.
        lighting_level: Supplemental lighting dim level, 0.0-1.0.
        battery: Whether to charge, discharge or leave the battery idle.
        battery_power_kw: Magnitude of the battery request in kW. Ignored when
            ``battery`` is ``"idle"``. Always non-negative; direction is carried
            by ``battery``.
        chp_mode: ``"off"``, ``"heat_led"`` (follow heat demand) or
            ``"max_export"`` (run flat out and sell the surplus).
        co2_source: Where CO2 enrichment comes from this hour.
        reasoning: Free text shown to the human approver for this interval (R22).
    """

    hour: int
    heat_source: HeatSource = "boiler"
    lighting_level: float = 0.0
    battery: BatteryAction = "idle"
    battery_power_kw: float = 0.0
    chp_mode: ChpMode = "off"
    co2_source: Co2Source = "none"
    reasoning: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.hour, int)
            or isinstance(self.hour, bool)
            or not 0 <= self.hour < HOURS_PER_DAY
        ):
            raise IntentSchemaError(f"hour must be an int in 0..23, got {self.hour!r}")
        if self.heat_source not in HEAT_SOURCES:
            raise IntentSchemaError(
                f"hour {self.hour}: heat_source must be one of {HEAT_SOURCES}, "
                f"got {self.heat_source!r}"
            )
        if self.battery not in BATTERY_ACTIONS:
            raise IntentSchemaError(
                f"hour {self.hour}: battery must be one of {BATTERY_ACTIONS}, got {self.battery!r}"
            )
        if self.chp_mode not in CHP_MODES:
            raise IntentSchemaError(
                f"hour {self.hour}: chp_mode must be one of {CHP_MODES}, got {self.chp_mode!r}"
            )
        if self.co2_source not in CO2_SOURCES:
            raise IntentSchemaError(
                f"hour {self.hour}: co2_source must be one of {CO2_SOURCES}, "
                f"got {self.co2_source!r}"
            )
        try:
            lighting = float(self.lighting_level)
        except (TypeError, ValueError) as exc:
            raise IntentSchemaError(
                f"hour {self.hour}: lighting_level must be a finite number, "
                f"got {self.lighting_level!r}"
            ) from exc
        if isinstance(self.lighting_level, bool) or not math.isfinite(lighting):
            raise IntentSchemaError(
                f"hour {self.hour}: lighting_level must be a finite number, "
                f"got {self.lighting_level!r}"
            )
        if not 0.0 <= lighting <= 1.0:
            raise IntentSchemaError(
                f"hour {self.hour}: lighting_level must be in [0, 1], got {self.lighting_level!r}"
            )
        try:
            battery_power = float(self.battery_power_kw)
        except (TypeError, ValueError) as exc:
            raise IntentSchemaError(
                f"hour {self.hour}: battery_power_kw must be a finite number, "
                f"got {self.battery_power_kw!r}"
            ) from exc
        if isinstance(self.battery_power_kw, bool) or not math.isfinite(battery_power):
            raise IntentSchemaError(
                f"hour {self.hour}: battery_power_kw must be a finite number, "
                f"got {self.battery_power_kw!r}"
            )
        if battery_power < 0.0:
            raise IntentSchemaError(
                f"hour {self.hour}: battery_power_kw must be non-negative "
                f"(direction is carried by `battery`), got {self.battery_power_kw!r}"
            )

    @property
    def battery_signed_kw(self) -> float:
        """Battery request as a signed value: positive charges, negative discharges."""
        if self.battery == "charge":
            return float(self.battery_power_kw)
        if self.battery == "discharge":
            return -float(self.battery_power_kw)
        return 0.0


@dataclass(frozen=True)
class Plan:
    """One day of hourly energy intent, plus the context it was produced under."""

    date: str
    intervals: tuple[IntervalIntent, ...]
    planner: str = "unknown"
    brief: str = ""
    revision: int = 0
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.intervals) != HOURS_PER_DAY:
            raise IntentSchemaError(
                f"a plan must contain exactly {HOURS_PER_DAY} intervals, got {len(self.intervals)}"
            )
        hours = [iv.hour for iv in self.intervals]
        if hours != list(range(HOURS_PER_DAY)):
            raise IntentSchemaError(
                f"plan intervals must cover hours 0..23 exactly once, in order; got {hours}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        """Build a plan from a plain dict, e.g. parsed language-model output.

        Raises:
            IntentSchemaError: if the payload is not a well-formed plan.
        """
        if not isinstance(data, dict):
            raise IntentSchemaError(f"plan payload must be an object, got {type(data).__name__}")
        raw_intervals = data.get("intervals")
        if not isinstance(raw_intervals, list):
            raise IntentSchemaError("plan payload must contain a list under 'intervals'")

        known = {f for f in IntervalIntent.__dataclass_fields__}
        intervals = []
        for i, raw in enumerate(raw_intervals):
            if not isinstance(raw, dict):
                raise IntentSchemaError(f"interval {i} must be an object, got {type(raw).__name__}")
            unknown = set(raw) - known
            if unknown:
                raise IntentSchemaError(
                    f"interval {i} has unknown field(s): {sorted(unknown)}; "
                    f"allowed fields are {sorted(known)}"
                )
            payload = dict(raw)
            payload.setdefault("hour", i)
            try:
                payload["hour"] = int(payload["hour"])
            except (TypeError, ValueError) as exc:
                raise IntentSchemaError(f"interval {i}: hour is not an integer") from exc
            for numeric in ("lighting_level", "battery_power_kw"):
                if numeric in payload:
                    if isinstance(payload[numeric], bool):
                        raise IntentSchemaError(f"interval {i}: {numeric} is not a number")
                    try:
                        payload[numeric] = float(payload[numeric])
                    except (TypeError, ValueError) as exc:
                        raise IntentSchemaError(f"interval {i}: {numeric} is not a number") from exc
                    if not math.isfinite(payload[numeric]):
                        raise IntentSchemaError(f"interval {i}: {numeric} must be finite")
            intervals.append(IntervalIntent(**payload))

        return cls(
            date=str(data.get("date", "")),
            intervals=tuple(intervals),
            planner=str(data.get("planner", "unknown")),
            brief=str(data.get("brief", "")),
            revision=int(data.get("revision", 0)),
            notes=str(data.get("notes", "")),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def from_json(cls, text: str) -> Plan:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntentSchemaError(f"plan is not valid JSON: {exc}") from exc
        return cls.from_dict(payload)

    def replace_interval(self, hour: int, **changes: Any) -> Plan:
        """Return a copy with one interval edited. Used by the human-edit path (R23)."""
        from dataclasses import replace

        if not 0 <= hour < HOURS_PER_DAY:
            raise IntentSchemaError(f"hour must be in 0..23, got {hour}")
        edited = tuple(replace(iv, **changes) if iv.hour == hour else iv for iv in self.intervals)
        return replace(self, intervals=edited, revision=self.revision + 1)


def flat_plan(date: str, planner: str = "flat", **interval_kwargs: Any) -> Plan:
    """A 24-hour plan with every interval identical. Convenient for tests and defaults."""
    return Plan(
        date=date,
        planner=planner,
        intervals=tuple(IntervalIntent(hour=h, **interval_kwargs) for h in range(HOURS_PER_DAY)),
    )
