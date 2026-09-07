"""What every planner sees and what every planner returns.

The comparison at the heart of the study is only fair if the rule-based baseline,
the MPC reference and the language-model planner all receive the same information
and produce the same kind of output. :class:`PlanningContext` is that shared input,
and :class:`~kasflex.intent.Plan` is that shared output.

The context contains **forecasts only**. Realised conditions never enter it. This
is enforced by construction rather than by convention: the runner builds the
context from the forecast series and keeps the actuals to itself until scoring
(R4, ADR-0005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from kasflex.checker.verdict import Verdict
from kasflex.energy.assets import EnergyHub
from kasflex.energy.dispatch import HourlyConditions
from kasflex.intent import Plan


@dataclass(frozen=True)
class PlanningContext:
    """Everything a planner is allowed to know when it plans a day."""

    date: str
    forecast: tuple[HourlyConditions, ...]
    """Forecast conditions, one per hour. Never the realised values."""
    hub: EnergyHub
    brief: str = ""
    """Plain-language operator brief: price ceiling, light target, planned events (R24)."""
    previous_verdict: Verdict | None = None
    """The rejection being revised, on the second and later attempts (R18)."""
    previous_plan: Plan | None = None
    revision: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def prices(self) -> tuple[float, ...]:
        return tuple(c.power_price_eur_kwh for c in self.forecast)

    def cheapest_hours(self, n: int) -> list[int]:
        """Hours with the lowest forecast power price, cheapest first."""
        return [c.hour for c in sorted(self.forecast, key=lambda c: c.power_price_eur_kwh)[:n]]

    def dearest_hours(self, n: int) -> list[int]:
        """Hours with the highest forecast power price, dearest first."""
        return [
            c.hour
            for c in sorted(self.forecast, key=lambda c: -c.power_price_eur_kwh)[:n]
        ]


@runtime_checkable
class Planner(Protocol):
    """A planner turns a :class:`PlanningContext` into a :class:`Plan`."""

    name: str

    def plan(self, context: PlanningContext) -> Plan:
        ...
