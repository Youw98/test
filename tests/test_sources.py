"""Parsing live API responses, tested against recorded fixtures.

The build environment cannot reach ENTSO-E or Open-Meteo, so the HTTP calls
themselves are unverified. Everything that happens to a response once it arrives is
tested here, including the two format details most likely to produce a plausible
but wrong price curve: sparse point series and EUR/MWh units.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from kasflex.data.sources import (
    DstDayError,
    FetchError,
    local_day_bounds,
    openmeteo_source_for,
    parse_entsoe_day_ahead,
    parse_openmeteo_hourly,
)

FIXTURES = Path(__file__).parent / "fixtures"
DAY = dt.date(2023, 1, 15)


@pytest.fixture
def entsoe_xml() -> str:
    return (FIXTURES / "entsoe_a44_sparse.xml").read_text()


@pytest.fixture
def openmeteo_json() -> str:
    return (FIXTURES / "openmeteo_forecast.json").read_text()


# --- ENTSO-E ---------------------------------------------------------------


def test_parses_24_hours(entsoe_xml):
    rows = parse_entsoe_day_ahead(entsoe_xml, DAY)
    assert [r["hour"] for r in rows] == list(range(24))


def test_converts_eur_per_mwh_to_eur_per_kwh(entsoe_xml):
    """90.12 EUR/MWh is 0.09012 EUR/kWh. A factor of 1000 here would be invisible
    in the plan and wrong in every euro figure the project reports."""
    rows = parse_entsoe_day_ahead(entsoe_xml, DAY)
    assert rows[0]["price_eur_kwh"] == pytest.approx(0.09012)
    assert all(0.0 <= r["price_eur_kwh"] < 5.0 for r in rows)


def test_expands_sparse_positions(entsoe_xml):
    """ENTSO-E omits a position when its price repeats. The fixture omits 4-8."""
    rows = parse_entsoe_day_ahead(entsoe_xml, DAY)
    assert [r["price_eur_kwh"] for r in rows[2:8]] == [pytest.approx(0.07931)] * 6
    # Position 17 (hour 16) is present again and must take over.
    assert rows[16]["price_eur_kwh"] == pytest.approx(0.22011)


def test_acknowledgement_is_explained_not_crashed():
    xml = (FIXTURES / "entsoe_acknowledgement.xml").read_text()
    with pytest.raises(FetchError, match="acknowledgement"):
        parse_entsoe_day_ahead(xml, DAY)


def test_rejects_non_xml():
    with pytest.raises(FetchError, match="not valid XML"):
        parse_entsoe_day_ahead("503 Service Unavailable", DAY)


def test_an_html_error_page_is_named_as_such():
    """Valid XML, wrong document. Saying "no prices published yet" here would send
    an operator looking in entirely the wrong place."""
    with pytest.raises(FetchError, match="got <html>"):
        parse_entsoe_day_ahead("<html><body>503 Service Unavailable</body></html>", DAY)


def test_rejects_a_document_with_no_points():
    empty = (
        '<Publication_MarketDocument xmlns="urn:x"><TimeSeries><Period>'
        "<resolution>PT60M</resolution></Period></TimeSeries></Publication_MarketDocument>"
    )
    with pytest.raises(FetchError, match="no price points"):
        parse_entsoe_day_ahead(empty, DAY)


def test_rejects_non_hourly_resolution():
    quarter = (
        '<Publication_MarketDocument xmlns="urn:x"><TimeSeries><Period>'
        "<resolution>PT15M</resolution>"
        "<Point><position>1</position><price.amount>50.0</price.amount></Point>"
        "</Period></TimeSeries></Publication_MarketDocument>"
    )
    with pytest.raises(FetchError, match="hourly"):
        parse_entsoe_day_ahead(quarter, DAY)


# --- Open-Meteo ------------------------------------------------------------


def test_parses_weather(openmeteo_json):
    rows = parse_openmeteo_hourly(openmeteo_json, DAY)
    assert len(rows) == 24
    assert [r["hour"] for r in rows] == list(range(24))
    assert all(r["irradiance_w_m2"] >= 0 for r in rows)


def test_accepts_a_decoded_dict(openmeteo_json):
    assert parse_openmeteo_hourly(json.loads(openmeteo_json), DAY) == parse_openmeteo_hourly(
        openmeteo_json, DAY
    )


def test_ignores_hours_from_other_days(openmeteo_json):
    """The API returns whole days and can return more than one."""
    payload = json.loads(openmeteo_json)
    payload["hourly"]["time"] += [f"2023-01-16T{h:02d}:00" for h in range(24)]
    payload["hourly"]["temperature_2m"] += [9.9] * 24
    payload["hourly"]["shortwave_radiation"] += [1.0] * 24
    rows = parse_openmeteo_hourly(payload, DAY)
    assert len(rows) == 24
    assert all(r["outdoor_temp_c"] != 9.9 for r in rows)


def test_reports_an_api_error():
    with pytest.raises(FetchError, match="Open-Meteo reported an error"):
        parse_openmeteo_hourly(
            json.dumps({"error": True, "reason": "Value out of allowed range"}), DAY
        )


def test_requires_the_expected_variables():
    with pytest.raises(FetchError, match="missing time"):
        parse_openmeteo_hourly(json.dumps({"hourly": {"time": ["2023-01-15T00:00"]}}), DAY)


def test_rejects_a_short_day(openmeteo_json):
    payload = json.loads(openmeteo_json)
    for key in ("time", "temperature_2m", "shortwave_radiation"):
        payload["hourly"][key] = payload["hourly"][key][:20]
    with pytest.raises(FetchError, match="returned 20 hours"):
        parse_openmeteo_hourly(payload, DAY)


def test_negative_irradiance_is_clamped(openmeteo_json):
    payload = json.loads(openmeteo_json)
    payload["hourly"]["shortwave_radiation"][3] = -5.0
    assert parse_openmeteo_hourly(payload, DAY)[3]["irradiance_w_m2"] == 0.0


def test_past_forecast_uses_the_historical_forecast_dataset():
    assert openmeteo_source_for(DAY).dataset_key == "openmeteo_hist_forecast"


def test_future_forecast_and_historical_weather_keep_distinct_provenance():
    future = dt.date(9999, 1, 1)
    assert openmeteo_source_for(future).dataset_key == "openmeteo_forecast"
    assert openmeteo_source_for(future, archive=True).dataset_key == "openmeteo_archive"


# --- time ------------------------------------------------------------------


def test_normal_day_is_24_hours():
    start, end = local_day_bounds(DAY)
    assert (end - start).total_seconds() == 24 * 3600


@pytest.mark.parametrize("day", [dt.date(2023, 3, 26), dt.date(2023, 10, 29)])
def test_clock_change_days_are_refused(day):
    """23 and 25-hour days would corrupt the schedule silently. Better to stop."""
    with pytest.raises(DstDayError, match="hours long"):
        local_day_bounds(day)
