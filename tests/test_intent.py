"""The intent schema is the planner's contract; it must reject anything malformed."""

from __future__ import annotations

import math

import pytest

from kasflex.intent import HOURS_PER_DAY, IntentSchemaError, IntervalIntent, Plan, flat_plan


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
def test_non_finite_or_boolean_numeric_intent_is_rejected(value):
    with pytest.raises(IntentSchemaError):
        IntervalIntent(hour=0, battery_power_kw=value)
    with pytest.raises(IntentSchemaError):
        IntervalIntent(hour=0, lighting_level=value)


def test_roundtrip_json():
    plan = flat_plan("2023-01-15", heat_source="chp", lighting_level=0.5)
    assert Plan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"hour": 24}, "0..23"),
        ({"hour": 0, "heat_source": "nuclear"}, "heat_source"),
        ({"hour": 0, "battery": "sideways"}, "battery"),
        ({"hour": 0, "chp_mode": "turbo"}, "chp_mode"),
        ({"hour": 0, "co2_source": "magic"}, "co2_source"),
        ({"hour": 0, "lighting_level": 1.5}, "lighting_level"),
        ({"hour": 0, "lighting_level": -0.1}, "lighting_level"),
        ({"hour": 0, "battery_power_kw": -5.0}, "non-negative"),
    ],
)
def test_rejects_malformed_interval(kwargs, fragment):
    with pytest.raises(IntentSchemaError, match=fragment):
        IntervalIntent(**kwargs)


def test_rejects_wrong_number_of_intervals():
    with pytest.raises(IntentSchemaError, match="exactly 24"):
        Plan(date="d", intervals=tuple(IntervalIntent(hour=h) for h in range(12)))


def test_rejects_out_of_order_hours():
    intervals = [IntervalIntent(hour=h) for h in range(HOURS_PER_DAY)]
    intervals[3], intervals[9] = intervals[9], intervals[3]
    with pytest.raises(IntentSchemaError, match="0..23 exactly once"):
        Plan(date="d", intervals=tuple(intervals))


def test_rejects_unknown_field():
    """A hallucinated field must fail loudly rather than being silently dropped."""
    payload = {"intervals": [{"hour": h, "solar_sails": True} for h in range(24)]}
    with pytest.raises(IntentSchemaError, match="unknown field"):
        Plan.from_dict(payload)


def test_rejects_non_json():
    with pytest.raises(IntentSchemaError, match="not valid JSON"):
        Plan.from_json("I have decided not to produce a plan today.")


def test_battery_sign_convention():
    assert IntervalIntent(hour=0, battery="charge", battery_power_kw=100).battery_signed_kw == 100
    discharging = IntervalIntent(hour=0, battery="discharge", battery_power_kw=100)
    assert discharging.battery_signed_kw == -100
    assert IntervalIntent(hour=0, battery="idle", battery_power_kw=100).battery_signed_kw == 0


def test_edit_bumps_revision():
    plan = flat_plan("2023-01-15")
    edited = plan.replace_interval(5, lighting_level=0.9)
    assert edited.intervals[5].lighting_level == 0.9
    assert edited.intervals[4].lighting_level == 0.0
    assert edited.revision == plan.revision + 1
