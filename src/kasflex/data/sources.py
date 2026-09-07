"""Fetching the live series KasFlex runs on: prices and weather.

Design constraints, in the order they mattered:

* **Nothing reads a live API at run time.** These functions populate the cache; the
  scenario reads the cache (R30). A demonstration that needs the network is a
  demonstration that will fail in front of the people you most wanted to impress.
* **No new heavy dependencies.** Parsing is stdlib ``urllib`` plus
  ``xml.etree`` and ``json``, so fetching does not drag in requests, pandas or
  entsoe-py. ``entsoe-py`` remains a perfectly good alternative for interactive
  work; it is just not needed for a scheduled job that wants a small footprint.
* **Parsers are pure and separate from transport.** :func:`parse_entsoe_day_ahead`
  and :func:`parse_openmeteo_hourly` take text and return rows. That is what makes
  them testable without a network, which matters here: the environment this was
  built in cannot reach either service.

.. warning::

   The HTTP layer in this module has **not** been exercised against the live
   services, because the build environment's egress policy blocks
   ``api.open-meteo.com``, ``archive-api.open-meteo.com`` and
   ``web-api.tp.entsoe.eu``. The parsers, the caching, the retry logic and
   everything downstream are covered by tests against recorded responses. The
   request construction is written from the published API contracts and should be
   confirmed against the real services on first use -- run
   ``kasflex fetch --date <yesterday>`` and check the output before trusting a
   scheduled job.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as Date
from typing import Any
from zoneinfo import ZoneInfo

HOURS = 24
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

ENTSOE_ENDPOINT = "https://web-api.tp.entsoe.eu/api"
ENTSOE_NL_ZONE = "10YNL----------L"
"""EIC code for the Dutch bidding zone."""

OPENMETEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

# Westland, the Dutch glasshouse cluster the scenarios are built around.
DEFAULT_LAT = 51.99
DEFAULT_LON = 4.25


class FetchError(RuntimeError):
    """A source could not be fetched or its response could not be understood."""


class DstDayError(FetchError):
    """The requested local day does not have 24 hours.

    KasFlex models a day as exactly 24 hourly intervals throughout. On the two
    clock-change days a Dutch local day has 23 or 25, and silently dropping or
    duplicating an hour would corrupt the prices and the schedule without anything
    visibly failing. Raising is the honest response; handling short and long days
    properly is a real piece of work and is not pretended here.
    """


@dataclass(frozen=True)
class SourceMeta:
    """Where a series came from, for the cache manifest."""

    source: str
    licence: str
    dataset_key: str


ENTSOE_META = SourceMeta(
    source="https://web-api.tp.entsoe.eu/api (documentType=A44)",
    licence="ENTSO-E Transparency Platform terms of use",
    dataset_key="entsoe_da",
)
OPENMETEO_FORECAST_META = SourceMeta(
    source=OPENMETEO_FORECAST,
    licence="CC-BY 4.0 (Open-Meteo free tier)",
    dataset_key="openmeteo_hist_forecast",
)
OPENMETEO_ARCHIVE_META = SourceMeta(
    source=OPENMETEO_ARCHIVE,
    licence="CC-BY 4.0 (Open-Meteo free tier)",
    dataset_key="knmi_hourly",
)


# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------


def local_day_bounds(day: Date) -> tuple[datetime, datetime]:
    """UTC instants bounding a local calendar day.

    Raises:
        DstDayError: if the local day is not 24 hours long.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_TZ)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TZ)
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    hours = (end_utc - start_utc).total_seconds() / 3600.0
    if abs(hours - HOURS) > 1e-9:
        raise DstDayError(
            f"{day.isoformat()} is {hours:.0f} hours long in Europe/Amsterdam, not 24. "
            f"KasFlex models a day as exactly 24 intervals, so this clock-change day "
            f"cannot be represented without corrupting the schedule. Pick another day."
        )
    return start_utc, end_utc


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def http_get(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 2.0,
    user_agent: str = "kasflex/0.1 (research; +https://github.com/youw98/test)",
) -> str:
    """GET a URL with query parameters, retrying transient failures.

    Retries on timeouts, connection errors and 5xx responses. A 4xx is not retried:
    a bad API key or a malformed request will fail the same way every time, and
    hammering a public service over it is rude as well as pointless.

    Raises:
        FetchError: if every attempt fails, carrying the last error.
    """
    query = urllib.parse.urlencode(params)
    full = f"{url}?{query}"
    last: Exception | None = None

    for attempt in range(retries):
        request = urllib.request.Request(full, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            if exc.code < 500:
                raise FetchError(
                    f"{url} returned HTTP {exc.code}. This will not succeed on a "
                    f"retry -- check the request and any API key. Response: {body}"
                ) from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc

        if attempt < retries - 1:
            time.sleep(backoff * (2**attempt))

    raise FetchError(
        f"{url} failed after {retries} attempts. Last error: {last}. "
        f"If this is a scheduled run, the cached data from a previous day is still "
        f"usable -- see kasflex.data.pipeline."
    )


# --------------------------------------------------------------------------
# ENTSO-E day-ahead prices
# --------------------------------------------------------------------------


def parse_entsoe_day_ahead(xml_text: str, day: Date) -> list[dict[str, float]]:
    """Parse an ENTSO-E A44 publication document into 24 hourly prices.

    Two details of the format matter and are easy to get wrong:

    * Prices are quoted in **EUR/MWh**; KasFlex works in EUR/kWh throughout.
    * The point series is **sparse**. ENTSO-E omits a position when its price
      repeats the previous one, so a document can legitimately contain far fewer
      than 24 points. Reading them positionally, or assuming 24, produces a price
      curve that is quietly wrong rather than obviously broken.

    Args:
        xml_text: The raw response body.
        day: The local calendar day expected, used only for the returned labels.

    Returns:
        24 rows of ``{"hour", "price_eur_kwh"}``, ordered by hour.

    Raises:
        FetchError: if the document is not parseable, reports no prices, or
            reports a resolution other than hourly.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"ENTSO-E response is not valid XML: {exc}") from exc

    def strip(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    root_tag = strip(root.tag)
    if root_tag not in {"Publication_MarketDocument", "Acknowledgement_MarketDocument"}:
        # An HTML error page is perfectly valid XML, so it parses and then reports
        # "no price points" -- which tells a scheduled job that prices are not
        # published yet when in fact the gateway returned a 5xx page. Naming the
        # actual document is the difference between a five-minute diagnosis and an
        # afternoon of one.
        raise FetchError(
            f"expected an ENTSO-E Publication_MarketDocument, got <{root_tag}>. "
            f"This is usually a proxy or gateway error page rather than an API "
            f"response. First 200 characters: {xml_text[:200]!r}"
        )

    if root_tag == "Acknowledgement_MarketDocument":
        reasons = [
            (e.findtext("./{*}text") or "").strip()
            for e in root.findall(".//{*}Reason")
        ]
        raise FetchError(
            "ENTSO-E returned an acknowledgement rather than data"
            + (f": {'; '.join(r for r in reasons if r)}" if any(reasons) else "")
            + ". A common cause is asking for a day whose prices are not published yet."
        )

    by_position: dict[int, float] = {}
    resolutions: set[str] = set()
    for period in root.iter():
        if strip(period.tag) != "Period":
            continue
        resolution = (period.findtext("./{*}resolution") or "").strip()
        if resolution:
            resolutions.add(resolution)
        for point in period:
            if strip(point.tag) != "Point":
                continue
            position = point.findtext("./{*}position")
            amount = point.findtext("./{*}price.amount")
            if position is None or amount is None:
                continue
            by_position[int(position)] = float(amount)

    if not by_position:
        raise FetchError(
            "ENTSO-E document contained no price points. Prices for the requested "
            "day may not have been published yet (Dutch day-ahead publishes around "
            "13:00 CET on the preceding day)."
        )
    if resolutions and not resolutions & {"PT60M", "PT1H"}:
        raise FetchError(
            f"expected hourly ENTSO-E data, got resolution(s) {sorted(resolutions)}. "
            f"KasFlex plans in whole hours."
        )

    # Expand the sparse series: a missing position repeats the one before it.
    rows: list[dict[str, float]] = []
    last = by_position[min(by_position)]
    for hour in range(HOURS):
        last = by_position.get(hour + 1, last)   # positions are 1-based
        rows.append({"hour": hour, "price_eur_kwh": round(last / 1000.0, 6)})
    return rows


def fetch_entsoe_day_ahead(
    day: Date,
    api_key: str,
    zone: str = ENTSOE_NL_ZONE,
    **http_kwargs: Any,
) -> list[dict[str, float]]:
    """Fetch day-ahead prices for a local calendar day.

    Args:
        day: Local calendar day.
        api_key: ENTSO-E Transparency Platform security token.
        zone: Bidding-zone EIC code. Defaults to the Netherlands.

    Raises:
        FetchError: on transport or parsing failure.
        DstDayError: on a clock-change day.
    """
    if not api_key:
        raise FetchError(
            "no ENTSO-E API key. Set ENTSOE_API_KEY in the environment; register "
            "free at https://transparency.entsoe.eu/ and request API access."
        )
    start_utc, end_utc = local_day_bounds(day)
    params = {
        "securityToken": api_key,
        "documentType": "A44",
        "in_Domain": zone,
        "out_Domain": zone,
        "periodStart": start_utc.strftime("%Y%m%d%H%M"),
        "periodEnd": end_utc.strftime("%Y%m%d%H%M"),
    }
    return parse_entsoe_day_ahead(http_get(ENTSOE_ENDPOINT, params, **http_kwargs), day)


# --------------------------------------------------------------------------
# Open-Meteo weather
# --------------------------------------------------------------------------


def parse_openmeteo_hourly(payload: str | dict[str, Any], day: Date) -> list[dict[str, float]]:
    """Parse an Open-Meteo hourly response into 24 rows of weather.

    Args:
        payload: Response body, or an already-decoded dict.
        day: The local calendar day expected. Rows for other days are ignored,
            since the API returns whole days and may return more than one.

    Returns:
        24 rows of ``{"hour", "outdoor_temp_c", "irradiance_w_m2"}``.

    Raises:
        FetchError: if the response is malformed or does not cover the day.
    """
    data = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(data, dict):
        raise FetchError("Open-Meteo response was not a JSON object")
    if "error" in data:
        raise FetchError(f"Open-Meteo reported an error: {data.get('reason', data['error'])}")

    hourly = data.get("hourly")
    if not isinstance(hourly, dict):
        raise FetchError("Open-Meteo response has no 'hourly' block")
    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    radiation = hourly.get("shortwave_radiation")
    if not (times and temps is not None and radiation is not None):
        raise FetchError(
            "Open-Meteo response is missing time, temperature_2m or "
            "shortwave_radiation; check the 'hourly' parameters in the request"
        )
    if not (len(times) == len(temps) == len(radiation)):
        raise FetchError("Open-Meteo returned mismatched series lengths")

    wanted = day.isoformat()
    rows: list[dict[str, float]] = []
    for stamp, temp, rad in zip(times, temps, radiation, strict=True):
        if not str(stamp).startswith(wanted):
            continue
        hour = int(str(stamp)[11:13])
        rows.append(
            {
                "hour": hour,
                "outdoor_temp_c": float(temp) if temp is not None else 0.0,
                "irradiance_w_m2": max(0.0, float(rad)) if rad is not None else 0.0,
            }
        )

    rows.sort(key=lambda r: r["hour"])
    if len(rows) != HOURS:
        raise FetchError(
            f"Open-Meteo returned {len(rows)} hours for {wanted}, expected {HOURS}. "
            f"Check the requested date range and timezone."
        )
    return rows


def fetch_openmeteo(
    day: Date,
    *,
    latitude: float = DEFAULT_LAT,
    longitude: float = DEFAULT_LON,
    archive: bool = False,
    **http_kwargs: Any,
) -> list[dict[str, float]]:
    """Fetch hourly weather for a local calendar day.

    Args:
        archive: When True, read the historical archive -- what actually happened.
            When False, read the forecast. These are two different requirements,
            not alternatives: the planner sees the forecast and results are scored
            against the archive (ADR-0005).

    Raises:
        FetchError: on transport or parsing failure.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m,shortwave_radiation",
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "timezone": "Europe/Amsterdam",
    }
    url = OPENMETEO_ARCHIVE if archive else OPENMETEO_FORECAST
    return parse_openmeteo_hourly(http_get(url, params, **http_kwargs), day)
