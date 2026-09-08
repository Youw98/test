"""A deliberately constraint-blind planner.

This exists for one reason: the headline measurement -- violations with the checker
disabled versus enabled -- cannot be demonstrated by a planner that never proposes
anything unsafe. The rule-based baseline is careful by construction, so with the
baseline alone both columns of the table read zero and the pipeline looks correct
while proving nothing.

:class:`NaivePlanner` chases the cheapest hours with no regard for the connection
contract, the state of charge or the CHP's minimum run time. It stands in for an
unverified planner in tests and in the offline demo, so that acceptance criterion 3
can be checked on a laptop with no API key.

It is **not a model of how a language model behaves**. Do not report its numbers as
an AI result. It is a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass

from kasflex.controllers.base import PlanningContext
from kasflex.intent import IntervalIntent, Plan


@dataclass
class NaivePlanner:
    """Deliberately poor, constraint-blind fixture for the negative-control arm."""

    name: str = "naive"
    light_hours: int = 14
    battery_power_kw: float = 5000.0
    """Far above any sane battery rating, so the power and state-of-charge checks
    both have something to catch."""

    def plan(self, context: PlanningContext) -> Plan:
        cheap = set(context.cheapest_hours(4))
        dear = set(context.dearest_hours(4))
        expensive_light = set(context.dearest_hours(self.light_hours))
        intervals = []
        for c in context.forecast:
            hour = c.hour
            intervals.append(
                IntervalIntent(
                    hour=hour,
                    heat_source="boiler",
                    lighting_level=1.0 if hour in expensive_light else 0.0,
                    battery=(
                        "charge" if hour in dear else "discharge" if hour in cheap else "idle"
                    ),
                    battery_power_kw=self.battery_power_kw if hour in (cheap | dear) else 0.0,
                    chp_mode="off",
                    co2_source="liquid" if c.irradiance_w_m2 > 20 else "none",
                    reasoning=(
                        f"price {c.power_price_eur_kwh:.3f}: deliberately poor "
                        f"negative-control schedule"
                    ),
                )
            )
        return Plan(
            date=context.date,
            intervals=tuple(intervals),
            planner=self.name,
            brief=context.brief,
            revision=context.revision,
            notes="Constraint-blind fixture planner. Not an AI result.",
        )
