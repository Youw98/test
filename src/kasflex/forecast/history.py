"""Building a training history from the system you are actually going to forecast.

A forecaster must be trained on the same quantity it will later predict. That sounds
too obvious to state until you notice how easy it is to get wrong here: the weather
generator produces a heat demand of its own, and the greenhouse model produces a
different one from the same weather. Train on the first and predict for a pipeline
driven by the second, and the model is accurate about the wrong system -- it will
score well in backtest and plan for a greenhouse that does not exist.

So the history is built by running the greenhouse model over past weather, which is
also what a real deployment has: a grower's history is their own meter readings, not
a simulator's idea of what the demand should have been.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from kasflex.adapters.greenhouse import GreenhouseModel
from kasflex.energy.dispatch import HourlyConditions
from kasflex.intent import Plan, flat_plan


@dataclass(frozen=True)
class MeteredDay:
    """One past day: the weather that was forecast, and the demand actually metered."""

    date: str
    forecast: tuple[HourlyConditions, ...]
    actual: tuple[HourlyConditions, ...]


def build_history(
    weather_days: Sequence,
    greenhouse: GreenhouseModel,
    floor_area_m2: float,
    operating_plan: Plan | None = None,
) -> list[MeteredDay]:
    """Run ``greenhouse`` over past weather to produce a metered demand history.

    Args:
        weather_days: Past days, each exposing ``date``, ``forecast`` and ``actual``
            condition series (:class:`~kasflex.data.synthetic.SyntheticDay` fits).
        greenhouse: The model whose demand the forecaster will later predict.
        floor_area_m2: Greenhouse area, for scaling.
        operating_plan: The day's operation, which affects demand -- lighting adds
            heat, withholding heat changes the trajectory. Defaults to a steady,
            plausible operating pattern, standing in for "how this grower normally
            runs", which is what the historical record would reflect.

    Returns:
        Days in the same order, with ``actual`` carrying the metered heat and CO2
        demand and ``forecast`` carrying the forecast weather. Note that the
        forecast series keeps the *weather* forecast but its demand fields are the
        model's demand under forecast weather, so the pair stays consistent with how
        :func:`kasflex.run.run_scenario` builds conditions.
    """
    plan = operating_plan or flat_plan(
        "history", heat_source="boiler", lighting_level=0.4, co2_source="liquid"
    )
    out: list[MeteredDay] = []
    for day in weather_days:
        realised = greenhouse.simulate_day(plan, tuple(day.actual), floor_area_m2)
        predicted = greenhouse.simulate_day(plan, tuple(day.forecast), floor_area_m2)
        out.append(
            MeteredDay(
                date=day.date,
                forecast=tuple(
                    replace(
                        c,
                        heat_demand_kw=predicted.heat_demand_kw[i],
                        co2_demand_kg_h=predicted.co2_demand_kg_h[i],
                    )
                    for i, c in enumerate(day.forecast)
                ),
                actual=tuple(
                    replace(
                        c,
                        heat_demand_kw=realised.heat_demand_kw[i],
                        co2_demand_kg_h=realised.co2_demand_kg_h[i],
                    )
                    for i, c in enumerate(day.actual)
                ),
            )
        )
    return out
