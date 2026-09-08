"""Registry of every external dataset KasFlex can use.

This module is the machine-readable half of the data provenance requirement (R34).
Nothing in KasFlex reads an external dataset without a :class:`DatasetRef` naming
its DOI or source URL, its licence and its access route, so a published result can
always be traced back to what it was computed from.

The registry is descriptive, not an acquisition layer: it says what a dataset is
and where it lives. Fetching happens in the optional ``kasflex[data]`` extra and
always writes through :class:`~kasflex.data.cache.DataCache`, which records the
retrieval date and a checksum.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Phase = Literal["mvp", "phase2", "optional"]


@dataclass(frozen=True)
class DatasetRef:
    """A citable external dataset or data service."""

    key: str
    title: str
    kind: Literal["measurement", "forecast", "price", "grid", "software"]
    source: str
    """DOI where one exists, otherwise the canonical landing page or API root."""
    licence: str
    phase: Phase
    access: str
    """How to obtain it in practice, including any credential the user must supply."""
    notes: str = ""
    verified_on: str = "2026-09-04"
    """Date the link and licence were last checked. The requirements document asks
    for this explicitly: repositories move -- power-grid-model has already moved out
    of the Alliander organisation into its own."""


DATASETS: dict[str, DatasetRef] = {
    d.key: d
    for d in (
        DatasetRef(
            key="agc2",
            title="Autonomous Greenhouse Challenge, Second Edition (2019)",
            kind="measurement",
            source="https://doi.org/10.4121/uuid:88d22c60-21b3-4ea8-90db-20249a5be2a7",
            licence=(
                "See 4TU.ResearchData landing page (CC-BY family); confirm before redistribution"
            ),
            phase="mvp",
            access="Manual download from 4TU.ResearchData; place under data/raw/agc2/",
            notes=(
                "Cherry tomato, six months, six high-tech compartments at Wageningen "
                "Research Bleiswijk. Five AI-controlled compartments against a reference "
                "compartment run by three commercial growers, which gives both the measured "
                "energy consumption for validation and a human control benchmark. "
                "Companion paper: Hemming et al., Sensors 2020. The 96 m2 compartment scale "
                "is the validation scale, not the scenario scale -- see ADR-0004."
            ),
        ),
        DatasetRef(
            key="entsoe_da",
            title="ENTSO-E day-ahead electricity prices, Dutch bidding zone",
            kind="price",
            source="https://transparency.entsoe.eu/",
            licence="ENTSO-E Transparency Platform terms; free with a registered API key",
            phase="mvp",
            access=(
                "entsoe-py (https://github.com/EnergieID/entsoe-py) with ENTSOE_API_KEY "
                "in the environment. Fetch once, then run from cache."
            ),
            notes=(
                "Replaces the fixed 0.30 EUR/kWh that GreenLight-Gym2 uses by default (R3). "
                "The Dutch feed had an outage in early 2026, which is why no demonstration "
                "may depend on a live call (R30)."
            ),
        ),
        DatasetRef(
            key="knmi_hourly",
            title="KNMI hourly measured weather (radiation, temperature, humidity, wind)",
            kind="measurement",
            source="https://www.knmi.nl/nederland-nu/klimatologie/uurgegevens",
            licence="KNMI open data",
            phase="mvp",
            access="KNMI open data API or the hourly-records download",
            notes=(
                "Required measured-weather source for valid out-of-sample scoring. "
                "Ingestion is not implemented yet; never shown to the planner -- ADR-0005."
            ),
        ),
        DatasetRef(
            key="openmeteo_forecast",
            title="Open-Meteo current weather forecast",
            kind="forecast",
            source="https://api.open-meteo.com/v1/forecast",
            licence="CC-BY 4.0 (Open-Meteo free tier)",
            phase="daily-provisional",
            access="HTTP API, no key required for the free tier",
            notes="Used for current and future daily runs; cached before planning.",
        ),
        DatasetRef(
            key="openmeteo_hist_forecast",
            title="Open-Meteo historical forecast archive",
            kind="forecast",
            source="https://open-meteo.com/en/docs/historical-forecast-api",
            licence="CC-BY 4.0 (non-commercial tier free)",
            phase="mvp",
            access="HTTP API, no key required for the free tier",
            notes=(
                "What was *believed* at planning time. This is what the planner sees. "
                "The archive does not reach back to the 2019-2020 AGC period, which is "
                "the reason for the two-period rule in ADR-0004."
            ),
        ),
        DatasetRef(
            key="openmeteo_archive",
            title="Open-Meteo archive (provisional realised-weather proxy)",
            kind="historical weather proxy",
            source="https://archive-api.open-meteo.com/v1/archive",
            licence="CC-BY 4.0 (Open-Meteo free tier)",
            phase="mvp-provisional",
            access="HTTP API, no key required for the free tier",
            notes=(
                "Used provisionally by kasflex daily. It is kept distinct from KNMI "
                "measured weather and must not be described as satisfying D7."
            ),
        ),
        DatasetRef(
            key="ttf_gas",
            title="TTF natural gas front-month settlement prices",
            kind="price",
            source="https://www.theice.com/products/27996665/Dutch-TTF-Gas-Futures",
            licence="Check redistribution terms before publishing derived series",
            phase="mvp",
            access=(
                "Public market data. Daily resolution is sufficient; KasFlex holds the "
                "gas price constant within a day."
            ),
        ),
        DatasetRef(
            key="netbeheer_congestion",
            title="Netbeheer Nederland capacity map (regional congestion status)",
            kind="grid",
            source="https://capaciteitskaart.netbeheernederland.nl/",
            licence="Check terms; used here only to parameterise scenarios",
            phase="phase2",
            access="Manual lookup of the scenario region's status",
            notes=(
                "Supplies the congestion windows in ContractLimits. In the MVP these are "
                "set by hand in the scenario YAML rather than fetched."
            ),
        ),
        DatasetRef(
            key="tennet_imbalance",
            title="TenneT imbalance and balancing prices",
            kind="price",
            source="https://developer.tennet.eu/",
            licence="TenneT developer portal terms",
            phase="optional",
            access="TenneT developer portal API",
            notes="Out of MVP scope. Relevant only if intraday response is added later.",
        ),
    )
}


def datasets_for_phase(phase: Phase) -> list[DatasetRef]:
    return [d for d in DATASETS.values() if d.phase == phase]


def as_markdown_table() -> str:
    """Render the registry as the provenance table in docs/DATA.md."""
    header = "| Key | Dataset | Kind | Phase | Licence | Source |\n|---|---|---|---|---|---|\n"
    rows = "".join(
        f"| `{d.key}` | {d.title} | {d.kind} | {d.phase} | {d.licence} | {d.source} |\n"
        for d in DATASETS.values()
    )
    return header + rows
