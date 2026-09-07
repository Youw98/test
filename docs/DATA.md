# Data and provenance

KasFlex reads no external dataset without a registry entry naming its DOI or source,
its licence and how it was obtained. The registry is machine-readable
(`kasflex.data.registry`), so this page and the code cannot drift apart —
`kasflex datasets --markdown` regenerates the table below.

## Registry

| Key | Dataset | Kind | Phase | Licence | Source |
|---|---|---|---|---|---|
| `agc2` | Autonomous Greenhouse Challenge, Second Edition (2019) | measurement | mvp | See 4TU.ResearchData landing page (CC-BY family); confirm before redistribution | https://doi.org/10.4121/uuid:88d22c60-21b3-4ea8-90db-20249a5be2a7 |
| `entsoe_da` | ENTSO-E day-ahead electricity prices, Dutch bidding zone | price | mvp | ENTSO-E Transparency Platform terms; free with a registered API key | https://transparency.entsoe.eu/ |
| `knmi_hourly` | KNMI hourly measured weather (radiation, temperature, humidity, wind) | measurement | mvp | KNMI open data | https://www.knmi.nl/nederland-nu/klimatologie/uurgegevens |
| `openmeteo_hist_forecast` | Open-Meteo historical forecast archive | forecast | mvp | CC-BY 4.0 (non-commercial tier free) | https://open-meteo.com/en/docs/historical-forecast-api |
| `ttf_gas` | TTF natural gas front-month settlement prices | price | mvp | Check redistribution terms before publishing derived series | https://www.theice.com/products/27996665/Dutch-TTF-Gas-Futures |
| `netbeheer_congestion` | Netbeheer Nederland capacity map (regional congestion status) | grid | phase2 | Check terms; used here only to parameterise scenarios | https://capaciteitskaart.netbeheernederland.nl/ |
| `tennet_imbalance` | TenneT imbalance and balancing prices | price | optional | TenneT developer portal terms | https://developer.tennet.eu/ |

## The rule that matters most

**D7 (KNMI measured) and D8 (Open-Meteo archived forecast) are two requirements,
not alternatives.**

The planner sees the forecast. Results are evaluated against what actually
happened. Confusing the two invalidates every result — a planner scored against the
weather it was given is an oracle, and its performance means nothing. The separation
is structural in KasFlex rather than conventional: `PlanningContext` carries only
forecast series, so a planner cannot reach the actuals even by accident. See
[DECISIONS.md](DECISIONS.md) ADR-0005.

The archived forecast (D8) does **not** reach back to the 2019–2020 AGC period.
That is the reason for the two-period rule: validate on 2019–2020 where AGC
measurements exist, run scenarios on 2022 onwards where volatility, congestion and
archived forecasts coexist. **Verify Open-Meteo's actual coverage before fixing
scenario dates.**

## Automated acquisition

`kasflex fetch` and `kasflex daily` populate the cache from ENTSO-E (day-ahead
prices) and Open-Meteo (forecast and archived weather). See
[deploy/README.md](../deploy/README.md) for scheduling.

Two format details in the ENTSO-E response are easy to get wrong and produce a
price curve that is plausible but incorrect:

* Prices are quoted in **EUR/MWh**, not EUR/kWh.
* The point series is **sparse** — a position is omitted when its price repeats the
  previous one, so a valid document can contain far fewer than 24 points. Reading
  them positionally gives the wrong hours the wrong prices.

Both are handled and both are covered by tests using a deliberately sparse fixture.

**Clock-change days are refused, not approximated.** A Dutch local day is 23 or 25
hours twice a year. KasFlex models a day as exactly 24 intervals throughout, and
silently dropping or duplicating an hour would corrupt the prices and the schedule
without anything visibly failing. `local_day_bounds` raises instead.

**Untested against the live services.** The build environment blocks
`web-api.tp.entsoe.eu`, `api.open-meteo.com` and `archive-api.open-meteo.com`, so
the request construction is written from the published API contracts and has not
been confirmed. Everything from the response onwards is tested.

## Caching

Nothing reads a live API at run time (R30). Every series is fetched once and cached
with its provenance:

```python
from kasflex.data import DataCache

cache = DataCache("data/cache")
cache.put(
    "entsoe_da_2023-01",
    rows,
    source="https://transparency.entsoe.eu/",
    licence="ENTSO-E Transparency Platform terms",
    dataset_key="entsoe_da",
)
```

Each entry records the source, licence, retrieval date, row count, columns and a
SHA-256 checksum, written to `data/cache/MANIFEST.json`. Reading verifies the
checksum and **raises** on a mismatch rather than returning data that no longer
matches its provenance record. Parquet is used when `pyarrow` is available and CSV
otherwise; the manifest records which.

The ENTSO-E Dutch feed had an outage in early 2026. A demonstration that depends on
a live call is a demonstration that will eventually fail in front of the people you
most wanted to impress.

## Running with no data at all

`kasflex.data.synthetic` generates a deterministic day: a recognisable Dutch
day-ahead price shape, a diurnal irradiance and heat-demand profile, and a forecast
that differs from the actual as a real forecast does. Same seed, same day, always.

This exists so that a new user can clone and run before obtaining an API key, and so
the test suite never touches the network. It is **not** a substitute for the real
series: runs using it are stamped `"data_source": "synthetic"` in their result
records.

## Licences of things we depend on

| Component | Licence | Consequence |
|---|---|---|
| KasFlex core | Apache-2.0 | Reusable by anyone, including commercially |
| `workers/greenlight/worker.py` | AGPL-3.0-or-later | Inherited from `gl-gym`; isolated to one file and its own venv |
| `gl-gym` | AGPL-3.0-or-later | See ADR-0001 |
| `power-grid-model` | MPL-2.0 | File-level copyleft; safe to depend on |
| `power-grid-model-ds` | MPL-2.0 | Same |
| Power-Agent repositories | MIT | Reference patterns only; no dependency taken |
| AGC datasets | 4TU.ResearchData terms | Confirm before redistributing any derived data |

**Verify every link and licence at project start.** Repositories move.
