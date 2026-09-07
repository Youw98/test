"""Shared fixtures. Everything here is offline and deterministic."""

from __future__ import annotations

import dataclasses

import pytest

from kasflex.adapters.greenhouse import SurrogateGreenhouse
from kasflex.data.synthetic import synthetic_day
from kasflex.energy.assets import EnergyHub
from kasflex.energy.dispatch import HourlyConditions
from kasflex.intent import flat_plan


@pytest.fixture
def hub() -> EnergyHub:
    return EnergyHub()


@pytest.fixture
def day():
    return synthetic_day("2023-01-15", seed=42)


@pytest.fixture
def conditions(hub, day) -> tuple[HourlyConditions, ...]:
    """Forecast conditions carrying a realistic heat and CO2 demand."""
    nominal = flat_plan("2023-01-15", heat_source="boiler", lighting_level=0.4)
    outcome = SurrogateGreenhouse().simulate_day(nominal, day.forecast, hub.floor_area_m2)
    return tuple(
        dataclasses.replace(
            c,
            heat_demand_kw=outcome.heat_demand_kw[i],
            co2_demand_kg_h=outcome.co2_demand_kg_h[i],
        )
        for i, c in enumerate(day.forecast)
    )
