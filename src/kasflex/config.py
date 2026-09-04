"""Scenario configuration: YAML in, typed objects out.

Everything that defines a run lives in one YAML file, so that a scenario can be
cited, diffed and reproduced. Unknown keys are rejected rather than ignored --
a silently misspelled ``import_limit_kw`` would quietly change every result in a
study, which is exactly the kind of error a reproducibility requirement exists to
prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from kasflex.checker.rules import CheckerConfig
from kasflex.energy.assets import (
    Battery,
    Boiler,
    Chp,
    ContractLimits,
    CropLimits,
    EnergyHub,
    HeatBuffer,
    Pv,
)


class ConfigError(ValueError):
    """Raised for a malformed or unrecognised configuration."""


def _build(cls: type, data: dict[str, Any] | None, where: str) -> Any:
    """Instantiate a dataclass from a mapping, rejecting unknown keys."""
    if not data:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)} if is_dataclass(cls) else set()
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {sorted(unknown)}. Valid keys are {sorted(known)}."
        )
    return cls(**data)


@dataclass(frozen=True)
class ScenarioConfig:
    """A complete, reproducible description of what to run."""

    name: str
    date: str
    hub: EnergyHub
    checker: CheckerConfig
    planner: str = "rule-based"
    greenhouse: str = "surrogate"
    seed: int = 0
    brief: str = ""
    data_source: str = "synthetic"
    """``synthetic`` or ``cache``. Never a live API call at run time (R30)."""
    winter: bool = True
    llm_model: str = "claude-opus-5"
    trace_path: str = "traces/planner.jsonl"
    audit_path: str = "results/audit.jsonl"
    greenlight_scenario: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> ScenarioConfig:
        """Load a scenario from a YAML file."""
        text = Path(path).read_text()
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        return cls.from_dict(data, where=str(path))

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "config") -> ScenarioConfig:
        data = dict(data)
        hub_data = dict(data.pop("hub", {}) or {})
        contract = _build(ContractLimits, hub_data.pop("contract", None), f"{where}.hub.contract")
        # YAML mapping keys arrive as strings; congestion windows are keyed by hour.
        if contract.congestion_windows:
            contract = ContractLimits(
                import_limit_kw=contract.import_limit_kw,
                export_limit_kw=contract.export_limit_kw,
                congestion_windows={
                    int(h): (float(v[0]), float(v[1]))
                    for h, v in contract.congestion_windows.items()
                },
            )
        hub = EnergyHub(
            **{
                k: v
                for k, v in hub_data.items()
                if k in {"floor_area_m2", "lamp_power_w_m2", "lamp_ppfd_umol_m2_s", "base_load_kw"}
            },
            contract=contract,
            battery=_build(Battery, hub_data.get("battery"), f"{where}.hub.battery"),
            chp=_build(Chp, hub_data.get("chp"), f"{where}.hub.chp"),
            boiler=_build(Boiler, hub_data.get("boiler"), f"{where}.hub.boiler"),
            buffer=_build(HeatBuffer, hub_data.get("buffer"), f"{where}.hub.buffer"),
            pv=_build(Pv, hub_data.get("pv"), f"{where}.hub.pv"),
            crop=_build(CropLimits, hub_data.get("crop"), f"{where}.hub.crop"),
        )
        leftover = set(hub_data) - {
            "floor_area_m2", "lamp_power_w_m2", "lamp_ppfd_umol_m2_s", "base_load_kw",
            "battery", "chp", "boiler", "buffer", "pv", "crop",
        }
        if leftover:
            raise ConfigError(f"{where}.hub: unknown key(s) {sorted(leftover)}")

        checker_data = dict(data.pop("checker", {}) or {})
        if "excluded_checks" in checker_data:
            checker_data["excluded_checks"] = tuple(checker_data["excluded_checks"] or ())
        checker = _build(CheckerConfig, checker_data, f"{where}.checker")

        known = {f.name for f in fields(cls)} - {"hub", "checker"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"{where}: unknown key(s) {sorted(unknown)}. Valid keys are {sorted(known)}."
            )
        if "name" not in data or "date" not in data:
            raise ConfigError(f"{where}: both 'name' and 'date' are required")
        return cls(hub=hub, checker=checker, **data)
