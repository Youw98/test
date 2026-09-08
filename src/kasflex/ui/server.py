"""A local web server for the KasFlex interface.

Built on ``http.server`` from the standard library. That is an unusual choice and a
deliberate one: the alternative is adding FastAPI, Starlette and uvicorn to a
project whose core dependencies are numpy and PyYAML, in order to serve one page to
one person on their own machine. The interface requirement is "browser-based, no
installation" (R27) -- a framework would work against that.

.. warning::

   **Localhost only, single user, no authentication.** This is a research tool that
   runs a simulation on the machine it is started on. It binds to 127.0.0.1 and
   should not be exposed to a network. If this ever needs to be multi-user or
   hosted, it needs a real framework and a real auth story; do not simply change
   the bind address.

The API is deliberately thin. Everything it does is a call into the same functions
the CLI uses, so the interface cannot drift from what a scripted run would produce.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import mimetypes
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kasflex.checker.rules import SafetyChecker
from kasflex.config import ConfigError, ScenarioConfig
from kasflex.intent import IntentSchemaError, IntervalIntent, Plan
from kasflex.oversight import AuditLog
from kasflex.resources import resolve_output, static_dir

STATIC_DIR = static_dir()

_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" rx="4" fill="#102a2a"/>'
    b'<path d="M2.8 8.1 8 3.4l5.2 4.7v4.6H2.8z" fill="none" stroke="#b8f34a" '
    b'stroke-width="1.4"/><path d="M8 3.4v9.3M4.4 6.7h7.2" stroke="#b8f34a" '
    b'stroke-width="1"/></svg>'
)

#: The settings the interface exposes. Everything else stays in the scenario file.
#:
#: This list is the answer to "what can I change from the UI". It is deliberately a
#: curated subset: the point is the handful of things an experiment actually varies,
#: not every field in the configuration. Each entry is
#: ``(path, label, kind, minimum, maximum, step, help)`` where ``path`` is a
#: dotted path into the scenario config.
ADJUSTABLE: tuple[dict[str, Any], ...] = (
    {
        "path": "planner",
        "label": "Planner",
        "kind": "choice",
        "choices": ["rule-based", "learned", "naive", "llm", "mpc"],
        "help": "Which planner proposes the day. 'learned' forecasts demand and optimises.",
    },
    {
        "path": "checker.enabled",
        "label": "Safety checker",
        "kind": "bool",
        "help": "Turn verification off to measure what it is worth. This is the experiment.",
    },
    {
        "path": "checker.explain",
        "label": "Explain rejections",
        "kind": "bool",
        "help": "Whether a rejected planner is told why. Separates verification from explanation.",
    },
    {
        "path": "checker.max_revisions",
        "label": "Revisions allowed",
        "kind": "int",
        "min": 0,
        "max": 10,
        "step": 1,
        "help": "How many times a planner may revise before the baseline takes over.",
    },
    {"path": "date", "label": "Date", "kind": "text", "help": "The day to simulate."},
    {
        "path": "seed",
        "label": "Seed",
        "kind": "int",
        "min": 0,
        "max": 9999,
        "step": 1,
        "help": "Same seed, same day, every time.",
    },
    {
        "path": "winter",
        "label": "Winter conditions",
        "kind": "bool",
        "help": "Winter: low light, high heat demand. Summer is the reverse.",
    },
    {
        "path": "hub.contract.import_limit_kw",
        "label": "Grid import limit",
        "kind": "number",
        "min": 500,
        "max": 20000,
        "step": 100,
        "unit": "kW",
        "help": "The connection contract. Exceeding it is a hard violation.",
    },
    {
        "path": "hub.contract.export_limit_kw",
        "label": "Grid export limit",
        "kind": "number",
        "min": 0,
        "max": 20000,
        "step": 100,
        "unit": "kW",
        "help": "Feed-in limit. Often lower than import, and zero under a non-firm contract.",
    },
    {
        "path": "hub.battery.capacity_kwh",
        "label": "Battery capacity",
        "kind": "number",
        "min": 0,
        "max": 20000,
        "step": 100,
        "unit": "kWh",
    },
    {
        "path": "hub.battery.max_charge_kw",
        "label": "Battery power",
        "kind": "number",
        "min": 0,
        "max": 10000,
        "step": 50,
        "unit": "kW",
        "help": "Applied to both charge and discharge.",
    },
    {
        "path": "hub.chp.electrical_capacity_kw",
        "label": "CHP size",
        "kind": "number",
        "min": 0,
        "max": 10000,
        "step": 100,
        "unit": "kWe",
    },
    {
        "path": "hub.chp.min_run_hours",
        "label": "CHP minimum run",
        "kind": "int",
        "min": 1,
        "max": 12,
        "step": 1,
        "unit": "h",
    },
    {
        "path": "hub.chp.min_down_hours",
        "label": "CHP minimum down",
        "kind": "int",
        "min": 1,
        "max": 12,
        "step": 1,
        "unit": "h",
    },
    {
        "path": "hub.buffer.capacity_kwh",
        "label": "Heat buffer",
        "kind": "number",
        "min": 0,
        "max": 40000,
        "step": 500,
        "unit": "kWh",
    },
    {
        "path": "hub.crop.dli_target_mol_m2",
        "label": "Light target",
        "kind": "number",
        "min": 0,
        "max": 30,
        "step": 0.5,
        "unit": "mol/m2",
        "help": "Supplemental daily light integral the crop needs.",
    },
    {
        "path": "hub.floor_area_m2",
        "label": "Greenhouse area",
        "kind": "number",
        "min": 96,
        "max": 200000,
        "step": 1000,
        "unit": "m2",
        "help": "Validation runs at 96 m2; scenarios at commercial scale. Do not mix them.",
    },
    {
        "path": "brief",
        "label": "Operator brief",
        "kind": "textarea",
        "help": "Plain language instruction passed to the planner.",
    },
)


class ApiError(Exception):
    """A request the server understood and refused, with an HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _get_path(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _coerce(spec: dict[str, Any], value: Any) -> Any:
    """Convert an incoming value to the declared kind and check its range.

    Raises:
        ApiError: naming the field, what arrived and what was expected.
    """
    kind, label = spec["kind"], spec["label"]
    try:
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "on"}
            return bool(value)
        if kind == "int":
            coerced: Any = int(value)
        elif kind == "number":
            coerced = float(value)
        elif kind == "choice":
            coerced = str(value)
            if coerced not in spec["choices"]:
                raise ApiError(f"{label}: {coerced!r} is not one of {spec['choices']}")
            return coerced
        else:
            return str(value)
    except ApiError:
        raise
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{label}: {value!r} is not a valid {kind}") from exc

    low, high = spec.get("min"), spec.get("max")
    if low is not None and coerced < low:
        raise ApiError(f"{label}: {coerced} is below the minimum of {low}")
    if high is not None and coerced > high:
        raise ApiError(f"{label}: {coerced} is above the maximum of {high}")
    return coerced


def _apply_overrides(config: ScenarioConfig, overrides: dict[str, Any]) -> ScenarioConfig:
    """Rebuild a scenario config with dotted-path overrides applied.

    Round-trips through :meth:`ScenarioConfig.from_dict` rather than mutating, so
    the interface gets exactly the same validation as a YAML file -- including the
    rejection of unknown keys. A UI that could set a field the config parser would
    refuse is a UI that can produce runs nobody can reproduce from a file.
    """
    spec = {entry["path"]: entry for entry in ADJUSTABLE}
    unknown = set(overrides) - set(spec)
    if unknown:
        raise ApiError(f"not adjustable from the interface: {sorted(unknown)}")

    # Coerce and range-check against the metadata the interface already publishes.
    # Dataclasses do not validate types, so without this a value of "three" for a
    # revision count is accepted here and fails much later inside the run, with an
    # error that says nothing about where it came from.
    overrides = {path: _coerce(spec[path], value) for path, value in overrides.items()}

    payload: dict[str, Any] = {
        "name": config.name,
        "date": config.date,
        "seed": config.seed,
        "winter": config.winter,
        "planner": config.planner,
        "greenhouse": config.greenhouse,
        "data_source": config.data_source,
        "brief": config.brief,
        "llm_model": config.llm_model,
        "history_days": config.history_days,
        "latitude": config.latitude,
        "longitude": config.longitude,
        "entsoe_zone": config.entsoe_zone,
        "gas_price_eur_kwh": config.gas_price_eur_kwh,
        "trace_path": config.trace_path,
        "audit_path": config.audit_path,
        "checker": {
            "enabled": config.checker.enabled,
            "explain": config.checker.explain,
            "max_revisions": config.checker.max_revisions,
            "excluded_checks": list(config.checker.excluded_checks),
            "fail_on_projected": config.checker.fail_on_projected,
        },
        "hub": {
            "floor_area_m2": config.hub.floor_area_m2,
            "lamp_power_w_m2": config.hub.lamp_power_w_m2,
            "lamp_ppfd_umol_m2_s": config.hub.lamp_ppfd_umol_m2_s,
            "base_load_kw": config.hub.base_load_kw,
            "contract": dataclasses.asdict(config.hub.contract),
            "battery": dataclasses.asdict(config.hub.battery),
            "chp": dataclasses.asdict(config.hub.chp),
            "boiler": dataclasses.asdict(config.hub.boiler),
            "buffer": dataclasses.asdict(config.hub.buffer),
            "pv": dataclasses.asdict(config.hub.pv),
            "crop": dataclasses.asdict(config.hub.crop),
        },
    }

    for path, value in overrides.items():
        target = payload
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value

    # Battery power is one control in the interface and two fields in the model.
    if "hub.battery.max_charge_kw" in overrides:
        payload["hub"]["battery"]["max_discharge_kw"] = overrides["hub.battery.max_charge_kw"]

    try:
        return ScenarioConfig.from_dict(payload, where="interface")
    except ConfigError as exc:
        raise ApiError(str(exc)) from exc


def _day_for(config: ScenarioConfig):
    from kasflex.data.synthetic import synthetic_day

    return synthetic_day(
        config.date,
        seed=config.seed,
        floor_area_m2=config.hub.floor_area_m2,
        winter=config.winter,
    )


def _conditions_for(config: ScenarioConfig, greenhouse, base, plan: Plan | None = None):
    """Attach the greenhouse's heat and CO2 demand to a weather series."""
    from kasflex.controllers.base import PlanningContext
    from kasflex.controllers.rule_based import RuleBasedPlanner

    if plan is None:
        plan = RuleBasedPlanner().plan(
            PlanningContext(date=config.date, forecast=tuple(base), hub=config.hub)
        )
    outcome = greenhouse.simulate_day(plan, tuple(base), config.hub.floor_area_m2)
    return tuple(
        dataclasses.replace(
            c,
            heat_demand_kw=outcome.heat_demand_kw[i],
            co2_demand_kg_h=outcome.co2_demand_kg_h[i],
        )
        for i, c in enumerate(base)
    ), outcome


def _plan_payload(plan: Plan, conditions, dispatch=None, contract=None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, iv in enumerate(plan.intervals):
        row: dict[str, Any] = {
            "hour": iv.hour,
            "heat_source": iv.heat_source,
            "lighting_level": iv.lighting_level,
            "battery": iv.battery,
            "battery_power_kw": iv.battery_power_kw,
            "chp_mode": iv.chp_mode,
            "co2_source": iv.co2_source,
            "reasoning": iv.reasoning,
            "power_price_eur_kwh": conditions[i].power_price_eur_kwh,
            "heat_demand_kw": round(conditions[i].heat_demand_kw, 1),
            "outdoor_temp_c": round(conditions[i].outdoor_temp_c, 1),
            "irradiance_w_m2": round(conditions[i].irradiance_w_m2, 1),
        }
        if contract is not None:
            import_limit, export_limit = contract.limits_at(iv.hour)
            row["import_limit_kw"] = round(import_limit, 1)
            row["export_limit_kw"] = round(export_limit, 1)
        if dispatch is not None:
            realised = dispatch.intervals[i]
            row.update(
                {
                    "grid_import_kw": round(realised.grid_import_kw, 1),
                    "grid_export_kw": round(realised.grid_export_kw, 1),
                    "grid_net_kw": round(realised.grid_net_kw, 1),
                    "battery_soc_kwh": round(realised.battery_soc_kwh, 1),
                    "buffer_level_kwh": round(realised.buffer_level_kwh, 1),
                    "energy_cost_eur": round(realised.energy_cost_eur, 2),
                }
            )
        rows.append(row)
    return rows


def _study_context(config: ScenarioConfig) -> dict[str, Any]:
    """Make the scale and period rules impossible to miss in the interface."""
    try:
        year = int(config.date[:4])
    except (TypeError, ValueError):
        year = 0
    research_scale = config.hub.floor_area_m2 <= 150
    if research_scale and year in {2019, 2020}:
        design_window = "validation"
        note = "AGC validation window: research-compartment scale and 2019-2020 data."
    elif not research_scale and year >= 2022:
        design_window = "scenario"
        note = "Commercial scenario window: scaled greenhouse and post-2022 conditions."
    else:
        design_window = "mixed"
        note = "Scale and period do not match either documented study window."
    return {
        "area_m2": config.hub.floor_area_m2,
        "scale": "research compartment" if research_scale else "commercial greenhouse",
        "period": design_window,
        "period_note": note,
        "data_source": config.data_source,
        "forecast_actual_separated": True,
        "offline": True,
    }


@dataclass
class UiServer:
    """Holds the scenario the interface is editing and serves the API."""

    config_path: str = "configs/scenario_westland_winter.yaml"
    anonymous: bool = False
    base: ScenarioConfig = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _approval_tokens: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _decision_tokens: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.base = ScenarioConfig.from_yaml(self.config_path)

    # -- endpoints ---------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        """The adjustable fields, their current values and their metadata."""
        fields = []
        for entry in ADJUSTABLE:
            item = dict(entry)
            item["value"] = _get_path(self.base, entry["path"])
            fields.append(item)
        return {
            "scenario": self.base.name,
            "config_path": self.config_path,
            "fields": fields,
        }

    def _issue_capabilities(
        self,
        plan: Plan,
        overrides: dict[str, Any],
        *,
        accepted: bool,
        checker_enabled: bool,
        source: str,
        replace: bool = True,
    ) -> tuple[str | None, str, str]:
        """Issue one-use capabilities tied to this exact plan snapshot.

        The browser disables approval after edits, but that is only a usability
        guard. Server-side tokens are the actual invariant: only a verified plan can
        be approved, while every rendered plan can be rejected. Both decisions stay
        bound to the plan and scenario the operator actually reviewed.
        """
        canonical = json.dumps(
            {"plan": plan.to_dict(), "overrides": overrides},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        approval_token = None
        decision_token = secrets.token_urlsafe(24)
        snapshot = {
            "fingerprint": fingerprint,
            "source": source,
            "overrides": dict(overrides),
            "issued_at": time.time(),
        }
        with self._lock:
            if replace:
                self._approval_tokens.clear()
                self._decision_tokens.clear()
            self._decision_tokens[decision_token] = snapshot
            if accepted and checker_enabled:
                approval_token = secrets.token_urlsafe(24)
                self._approval_tokens[approval_token] = snapshot
        return approval_token, decision_token, fingerprint

    def run(self, overrides: dict[str, Any], *, issue_approval: bool = True) -> dict[str, Any]:
        """Run one scenario and return everything the page needs to show it."""
        from kasflex.experiment import build_greenhouse, build_planner
        from kasflex.run import run_scenario

        config = _apply_overrides(self.base, overrides)
        day = _day_for(config)
        greenhouse = build_greenhouse(config.greenhouse, config)

        started = time.time()
        try:
            planner = build_planner(config.planner, config)
        except ValueError as exc:
            raise ApiError(str(exc)) from exc

        try:
            result = run_scenario(
                scenario=f"{config.name}/ui",
                date=config.date,
                hub=config.hub,
                forecast=day.forecast,
                actual=day.actual,
                planner=planner,
                greenhouse=greenhouse,
                checker_config=config.checker,
                audit_log=AuditLog(resolve_output(config.audit_path), anonymous=self.anonymous),
                brief=config.brief,
                seed=config.seed,
                provenance={"data_source": config.data_source, "via": "ui"},
            )
        except NotImplementedError as exc:
            raise ApiError(str(exc), status=501) from exc

        conditions, _ = _conditions_for(config, greenhouse, day.forecast, result.plan)
        hard = result.realised_hard_violations
        approval_token, decision_token, fingerprint = (
            self._issue_capabilities(
                result.plan,
                overrides,
                accepted=result.verdict.accepted,
                checker_enabled=result.checker_enabled,
                source="generated",
                replace=issue_approval,
            )
            if issue_approval
            else (None, "", "")
        )
        return {
            "date": result.date,
            "planner": result.planner,
            "greenhouse_model": result.greenhouse_model,
            "validated": result.outcome.validated,
            "checker_enabled": result.checker_enabled,
            "accepted": result.verdict.accepted,
            "fell_back": result.fell_back_to_baseline,
            "revisions_used": result.revisions_used,
            "feedback": result.verdict.feedback(explain=config.checker.explain),
            "violations": [v.to_dict() for v in result.verdict.violations],
            "checks_run": list(result.verdict.checks_run),
            "checks_excluded": list(result.verdict.checks_excluded),
            "realised_hard": hard,
            "realised_projected": len(result.realised_violations) - hard,
            "metrics": result.metrics,
            "plan": _plan_payload(result.plan, conditions, result.dispatch, config.hub.contract),
            "elapsed_s": round(time.time() - started, 2),
            "overrides": overrides,
            "context": _study_context(config),
            "approval_token": approval_token,
            "decision_token": decision_token,
            "plan_fingerprint": fingerprint,
        }

    def verify(self, overrides: dict[str, Any], plan_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Re-verify a plan a person has edited (R23).

        An edit is never accepted on the strength of having been made by a human.
        It goes back through exactly the same checker the planner's output did.
        """
        from kasflex.experiment import build_greenhouse

        config = _apply_overrides(self.base, overrides)
        # The rows sent back carry the display-only columns this server added when
        # it rendered the plan (price, heat demand). The intent schema rightly
        # rejects unknown fields, so strip them here rather than loosening the
        # schema: the strictness is what stops a planner inventing fields.
        intent_fields = set(IntervalIntent.__dataclass_fields__)
        cleaned = [{k: v for k, v in row.items() if k in intent_fields} for row in plan_rows]
        try:
            plan = Plan.from_dict(
                {"date": config.date, "planner": "human-edited", "intervals": cleaned}
            )
        except IntentSchemaError as exc:
            raise ApiError(f"edited plan is not valid: {exc}") from exc

        day = _day_for(config)
        greenhouse = build_greenhouse(config.greenhouse, config)
        conditions, outcome = _conditions_for(config, greenhouse, day.forecast, plan)
        verdict = SafetyChecker(config.hub, config.checker).verify(
            plan, conditions, outcome.projection()
        )

        from kasflex.energy.dispatch import dispatch_plan

        dispatch = dispatch_plan(plan, config.hub, list(conditions))
        metrics = {
            **dispatch.summary(),
            "fruit_growth_kg_m2": round(outcome.fruit_growth_kg_m2, 6),
            "natural_dli_mol_m2": round(outcome.natural_dli_mol_m2, 3),
            "supplemental_dli_mol_m2": round(dispatch.total_dli_mol_m2, 3),
            "temperature_band_hours": float(
                outcome.temperature_band_hours(
                    config.hub.crop.temp_min_c, config.hub.crop.temp_max_c
                )
            ),
            "heat_dumped_kwh": round(sum(iv.heat_dumped_kw for iv in dispatch.intervals), 2),
        }
        approval_token, decision_token, fingerprint = self._issue_capabilities(
            plan,
            overrides,
            accepted=verdict.accepted,
            checker_enabled=config.checker.enabled,
            source="human-edited",
        )
        return {
            "accepted": verdict.accepted,
            "feedback": verdict.feedback(explain=config.checker.explain),
            "violations": [v.to_dict() for v in verdict.violations],
            "checks_run": list(verdict.checks_run),
            "checks_excluded": list(verdict.checks_excluded),
            "metrics": metrics,
            "plan": _plan_payload(plan, conditions, dispatch, config.hub.contract),
            "context": _study_context(config),
            "approval_token": approval_token,
            "decision_token": decision_token,
            "plan_fingerprint": fingerprint,
        }

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a human decision in the append-only log (R25, R26)."""
        decision = str(payload.get("decision", "")).lower()
        if decision not in {"approve", "reject", "edit"}:
            raise ApiError("decision must be approve, reject or edit")
        approval_token = str(payload.get("approval_token", ""))
        decision_token = str(payload.get("decision_token", ""))
        decided_snapshot: dict[str, Any] | None = None
        with self._lock:
            if decision == "approve":
                decided_snapshot = self._approval_tokens.pop(approval_token, None)
                if decided_snapshot is None:
                    raise ApiError(
                        "This approval is stale or the plan was not verified. "
                        "Generate or re-verify the plan before approving."
                    )
                if decision_token:
                    decision_snapshot = self._decision_tokens.pop(decision_token, None)
                    if decision_snapshot is None:
                        raise ApiError(
                            "This decision is stale. Generate or re-verify the plan "
                            "before deciding."
                        )
                    if decision_snapshot["fingerprint"] != decided_snapshot["fingerprint"]:
                        raise ApiError(
                            "The decision and approval tokens identify different plans. "
                            "Generate or re-verify the plan before deciding."
                        )
                if payload.get("overrides", {}) != decided_snapshot["overrides"]:
                    raise ApiError(
                        "The scenario changed after this plan was verified. "
                        "Generate or re-verify it under the current settings."
                    )
                submitted_fingerprint = payload.get("plan_fingerprint")
                if (
                    submitted_fingerprint is not None
                    and submitted_fingerprint != decided_snapshot["fingerprint"]
                ):
                    raise ApiError(
                        "The plan fingerprint does not match the approved plan snapshot. "
                        "Generate or re-verify the plan before deciding."
                    )
            elif decision == "reject":
                decided_snapshot = self._decision_tokens.pop(decision_token, None)
                if decided_snapshot is None:
                    raise ApiError(
                        "This rejection is stale or does not identify the current plan. "
                        "Generate or re-verify the plan before rejecting."
                    )
                if payload.get("overrides", {}) != decided_snapshot["overrides"]:
                    raise ApiError(
                        "The scenario changed after this plan was generated or verified. "
                        "Generate or re-verify it under the current settings."
                    )
                if payload.get("plan_fingerprint") != decided_snapshot["fingerprint"]:
                    raise ApiError(
                        "The plan fingerprint does not match the current plan snapshot. "
                        "Generate or re-verify the plan before deciding."
                    )
            elif decision_token:
                decided_snapshot = self._decision_tokens.pop(decision_token, None)
            elif approval_token:
                decided_snapshot = self._approval_tokens.pop(approval_token, None)
            # A decision closes the current review; no older plan stays approvable.
            self._approval_tokens.clear()
            self._decision_tokens.clear()
        AuditLog(resolve_output(self.base.audit_path), anonymous=self.anonymous).append(
            "human_decision_ui",
            {
                "decision": decision,
                "comment": str(payload.get("comment", ""))[:2000],
                "seconds_to_decide": payload.get("seconds_to_decide"),
                "overrides": payload.get("overrides", {}),
                "plan_fingerprint": (
                    decided_snapshot.get("fingerprint") if decided_snapshot else None
                ),
                "verification_source": (
                    decided_snapshot.get("source") if decided_snapshot else None
                ),
            },
            operator=str(payload.get("operator", "")),
        )
        return {"recorded": True, "decision": decision, "anonymous": self.anonymous}

    def compare(self, overrides: dict[str, Any], planners: list[str]) -> dict[str, Any]:
        """Run several planners on the identical scenario (R29)."""
        rows = []
        for name in planners:
            try:
                rows.append({"planner": name, **self._compare_row(overrides, name)})
            except ApiError as exc:
                rows.append({"planner": name, "error": str(exc)})
        return {"rows": rows}

    def _compare_row(self, overrides: dict[str, Any], planner: str) -> dict[str, Any]:
        conditions = {
            "rule-based": ("rule-based", True),
            "learned": ("learned", True),
            "ai-unverified": ("naive", False),
            "ai-verified": ("naive", True),
            "mpc": ("mpc", True),
        }
        engine, checker_enabled = conditions.get(
            planner, (planner, bool(overrides.get("checker.enabled", self.base.checker.enabled)))
        )
        result = self.run(
            {**overrides, "planner": engine, "checker.enabled": checker_enabled},
            issue_approval=False,
        )
        return {
            "cost_eur": result["metrics"]["net_cost_eur"],
            "cost_eur_per_m2": result["metrics"]["net_cost_eur_per_m2"],
            "hard_violations": result["realised_hard"],
            "projected_violations": result["realised_projected"],
            "peak_import_kw": result["metrics"]["peak_import_kw"],
            "dli_mol_m2": result["metrics"]["supplemental_dli_mol_m2"],
            "band_hours": result["metrics"]["temperature_band_hours"],
            "growth_kg_m2": result["metrics"]["fruit_growth_kg_m2"],
            "fell_back": result["fell_back"],
            "accepted": result["accepted"],
            "engine": engine,
            "checker_enabled": checker_enabled,
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "kasflex"
    ui: UiServer

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        """Quieter than the default, which prints a line per asset request."""
        if "api" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError("request body must be a JSON object")
        return data

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            self._send(404, b"not found", "text/plain")
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), kind)

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/api/settings"):
                self._json(self.ui.get_settings())
            elif self.path.startswith("/favicon.ico"):
                # Answer rather than 404: a browser asks for this unprompted, and a
                # console full of red on first load makes a working page look broken.
                self._send(200, _FAVICON, "image/svg+xml")
            else:
                self._static(self.path.split("?")[0])
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._json(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-1500:],
                },
                500,
            )

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
            overrides = body.get("overrides", {})
            if self.path.startswith("/api/run"):
                self._json(self.ui.run(overrides))
            elif self.path.startswith("/api/verify"):
                self._json(self.ui.verify(overrides, body.get("plan", [])))
            elif self.path.startswith("/api/decision"):
                self._json(self.ui.decide(body))
            elif self.path.startswith("/api/compare"):
                self._json(
                    self.ui.compare(
                        overrides,
                        body.get("planners") or ["rule-based", "learned", "naive"],
                    )
                )
            else:
                self._json({"error": f"no such endpoint: {self.path}"}, 404)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._json(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-1500:],
                },
                500,
            )


def serve(
    config_path: str = "configs/scenario_westland_winter.yaml",
    host: str = "127.0.0.1",
    port: int = 8765,
    anonymous: bool = False,
) -> ThreadingHTTPServer:
    """Create the server. The caller decides whether to serve forever."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "KasFlex UI is a single-user research tool with no authentication; "
            "it may only bind to localhost."
        )
    ui = UiServer(config_path=config_path, anonymous=anonymous)
    handler = type("Handler", (_Handler,), {"ui": ui})
    return ThreadingHTTPServer((host, port), handler)
