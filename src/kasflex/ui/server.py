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
import json
import mimetypes
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kasflex.checker.rules import SafetyChecker
from kasflex.config import ConfigError, ScenarioConfig
from kasflex.intent import IntentSchemaError, IntervalIntent, Plan
from kasflex.oversight import AuditLog

STATIC_DIR = Path(__file__).parent / "static"

_FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" rx="3" fill="#2c5f2d"/>'
    b'<path d="M8 3.2 12.4 7v5.8H3.6V7z" fill="#97bc62"/></svg>'
)

#: The settings the interface exposes. Everything else stays in the scenario file.
#:
#: This list is the answer to "what can I change from the UI". It is deliberately a
#: curated subset: the point is the handful of things an experiment actually varies,
#: not every field in the configuration. Each entry is
#: ``(path, label, kind, minimum, maximum, step, help)`` where ``path`` is a
#: dotted path into the scenario config.
ADJUSTABLE: tuple[dict[str, Any], ...] = (
    {"path": "planner", "label": "Planner", "kind": "choice",
     "choices": ["rule-based", "learned", "naive", "llm", "mpc"],
     "help": "Which planner proposes the day. 'learned' forecasts demand and optimises."},
    {"path": "checker.enabled", "label": "Safety checker", "kind": "bool",
     "help": "Turn verification off to measure what it is worth. This is the experiment."},
    {"path": "checker.explain", "label": "Explain rejections", "kind": "bool",
     "help": "Whether a rejected planner is told why. Separates verification from explanation."},
    {"path": "checker.max_revisions", "label": "Revisions allowed", "kind": "int",
     "min": 0, "max": 10, "step": 1,
     "help": "How many times a planner may revise before the baseline takes over."},

    {"path": "date", "label": "Date", "kind": "text", "help": "The day to simulate."},
    {"path": "seed", "label": "Seed", "kind": "int", "min": 0, "max": 9999, "step": 1,
     "help": "Same seed, same day, every time."},
    {"path": "winter", "label": "Winter conditions", "kind": "bool",
     "help": "Winter: low light, high heat demand. Summer is the reverse."},

    {"path": "hub.contract.import_limit_kw", "label": "Grid import limit", "kind": "number",
     "min": 500, "max": 20000, "step": 100, "unit": "kW",
     "help": "The connection contract. Exceeding it is a hard violation."},
    {"path": "hub.contract.export_limit_kw", "label": "Grid export limit", "kind": "number",
     "min": 0, "max": 20000, "step": 100, "unit": "kW",
     "help": "Feed-in limit. Often lower than import, and zero under a non-firm contract."},

    {"path": "hub.battery.capacity_kwh", "label": "Battery capacity", "kind": "number",
     "min": 0, "max": 20000, "step": 100, "unit": "kWh"},
    {"path": "hub.battery.max_charge_kw", "label": "Battery power", "kind": "number",
     "min": 0, "max": 10000, "step": 50, "unit": "kW",
     "help": "Applied to both charge and discharge."},

    {"path": "hub.chp.electrical_capacity_kw", "label": "CHP size", "kind": "number",
     "min": 0, "max": 10000, "step": 100, "unit": "kWe"},
    {"path": "hub.chp.min_run_hours", "label": "CHP minimum run", "kind": "int",
     "min": 1, "max": 12, "step": 1, "unit": "h"},
    {"path": "hub.chp.min_down_hours", "label": "CHP minimum down", "kind": "int",
     "min": 1, "max": 12, "step": 1, "unit": "h"},

    {"path": "hub.buffer.capacity_kwh", "label": "Heat buffer", "kind": "number",
     "min": 0, "max": 40000, "step": 500, "unit": "kWh"},
    {"path": "hub.crop.dli_target_mol_m2", "label": "Light target", "kind": "number",
     "min": 0, "max": 30, "step": 0.5, "unit": "mol/m2",
     "help": "Supplemental daily light integral the crop needs."},
    {"path": "hub.floor_area_m2", "label": "Greenhouse area", "kind": "number",
     "min": 96, "max": 200000, "step": 1000, "unit": "m2",
     "help": "Validation runs at 96 m2; scenarios at commercial scale. Do not mix them."},

    {"path": "brief", "label": "Operator brief", "kind": "textarea",
     "help": "Plain language instruction passed to the planner."},
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
                raise ApiError(
                    f"{label}: {coerced!r} is not one of {spec['choices']}"
                )
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


def _conditions_for(config: ScenarioConfig, greenhouse, base):
    """Attach the greenhouse's heat and CO2 demand to a weather series."""
    from kasflex.controllers.base import PlanningContext
    from kasflex.controllers.rule_based import RuleBasedPlanner

    nominal = RuleBasedPlanner().plan(
        PlanningContext(date=config.date, forecast=tuple(base), hub=config.hub)
    )
    outcome = greenhouse.simulate_day(nominal, tuple(base), config.hub.floor_area_m2)
    return tuple(
        dataclasses.replace(
            c,
            heat_demand_kw=outcome.heat_demand_kw[i],
            co2_demand_kg_h=outcome.co2_demand_kg_h[i],
        )
        for i, c in enumerate(base)
    ), outcome


def _plan_payload(plan: Plan, conditions) -> list[dict[str, Any]]:
    return [
        {
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
        }
        for i, iv in enumerate(plan.intervals)
    ]


@dataclass
class UiServer:
    """Holds the scenario the interface is editing and serves the API."""

    config_path: str = "configs/scenario_westland_winter.yaml"
    anonymous: bool = False
    base: ScenarioConfig = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

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

    def run(self, overrides: dict[str, Any]) -> dict[str, Any]:
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
                audit_log=AuditLog(config.audit_path, anonymous=self.anonymous),
                brief=config.brief,
                seed=config.seed,
                provenance={"data_source": config.data_source, "via": "ui"},
            )
        except NotImplementedError as exc:
            raise ApiError(str(exc), status=501) from exc

        conditions, _ = _conditions_for(config, greenhouse, day.forecast)
        hard = result.realised_hard_violations
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
            "realised_hard": hard,
            "realised_projected": len(result.realised_violations) - hard,
            "metrics": result.metrics,
            "plan": _plan_payload(result.plan, conditions),
            "elapsed_s": round(time.time() - started, 2),
            "overrides": overrides,
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
        cleaned = [
            {k: v for k, v in row.items() if k in intent_fields} for row in plan_rows
        ]
        try:
            plan = Plan.from_dict({"date": config.date, "planner": "human-edited",
                                   "intervals": cleaned})
        except IntentSchemaError as exc:
            raise ApiError(f"edited plan is not valid: {exc}") from exc

        day = _day_for(config)
        greenhouse = build_greenhouse(config.greenhouse, config)
        conditions, outcome = _conditions_for(config, greenhouse, day.forecast)
        verdict = SafetyChecker(config.hub, config.checker).verify(
            plan, conditions, outcome.projection()
        )

        from kasflex.energy.dispatch import dispatch_plan

        dispatch = dispatch_plan(plan, config.hub, list(conditions))
        return {
            "accepted": verdict.accepted,
            "feedback": verdict.feedback(explain=config.checker.explain),
            "violations": [v.to_dict() for v in verdict.violations],
            "metrics": dispatch.summary(),
        }

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record a human decision in the append-only log (R25, R26)."""
        decision = str(payload.get("decision", "")).lower()
        if decision not in {"approve", "reject", "edit"}:
            raise ApiError("decision must be approve, reject or edit")
        AuditLog(self.base.audit_path, anonymous=self.anonymous).append(
            "human_decision_ui",
            {
                "decision": decision,
                "comment": str(payload.get("comment", ""))[:2000],
                "seconds_to_decide": payload.get("seconds_to_decide"),
                "overrides": payload.get("overrides", {}),
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
        result = self.run({**overrides, "planner": planner})
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
        self._send(status, json.dumps(payload, default=str).encode(), "application/json")

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
            self._json({"error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()[-1500:]}, 500)

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
                self._json(self.ui.compare(
                    overrides,
                    body.get("planners") or ["rule-based", "learned", "naive"],
                ))
            else:
                self._json({"error": f"no such endpoint: {self.path}"}, 404)
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()[-1500:]}, 500)


def serve(
    config_path: str = "configs/scenario_westland_winter.yaml",
    host: str = "127.0.0.1",
    port: int = 8765,
    anonymous: bool = False,
) -> ThreadingHTTPServer:
    """Create the server. The caller decides whether to serve forever."""
    ui = UiServer(config_path=config_path, anonymous=anonymous)
    handler = type("Handler", (_Handler,), {"ui": ui})
    return ThreadingHTTPServer((host, port), handler)
