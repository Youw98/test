"""Language-model planner, with recorded traces and offline replay (R9, R11, R12).

Three properties matter more here than raw planning quality:

* **Interchangeability.** Any of several models can be selected by configuration
  alone (R11), so a result is never an artefact of one vendor.
* **Replayability.** Every call is recorded, and a recorded run replays exactly
  with no API access and no key (R12). This is what makes the published figures
  reproducible by an external reviewer, and what keeps the test suite offline.
* **Containment.** The model's output is data, not instruction. It is parsed
  against the intent schema and then verified. A plan that does not parse is a
  schema error, not a crash, and the revision loop handles it like any other
  rejection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kasflex.controllers.base import PlanningContext
from kasflex.intent import HOURS_PER_DAY, IntentSchemaError, Plan

SYSTEM_PROMPT = """\
You plan one day of energy operation for a Dutch greenhouse.

You do NOT control actuators. You produce hourly energy intent. A deterministic
low-level controller turns your intent into actuator setpoints, and a deterministic
safety checker verifies your plan before anything runs. If the checker rejects your
plan you will be told which constraint, in which hour, with the actual value and the
bound, and you may revise.

Return ONLY a JSON object of this shape, with exactly 24 intervals, hours 0 to 23:

{
  "intervals": [
    {
      "hour": 0,
      "heat_source": "boiler" | "chp" | "buffer" | "none",
      "lighting_level": 0.0 to 1.0,
      "battery": "charge" | "discharge" | "idle",
      "battery_power_kw": number >= 0,
      "chp_mode": "off" | "heat_led" | "max_export",
      "co2_source": "chp" | "liquid" | "none",
      "reasoning": "one short sentence for the operator"
    }
  ]
}

Objective: minimise the cost of the day while keeping the crop within its limits and
staying inside the grid connection contract. Cheap hours are for charging and
lighting; expensive hours are for discharging, running the CHP, and drawing on the
heat buffer.
"""


@dataclass(frozen=True)
class RecordedTrace:
    """One recorded planner call: what went in, what came out (R25)."""

    key: str
    model: str
    prompt: str
    response: str
    revision: int
    date: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TraceStore:
    """Append-only JSONL store of planner calls, keyed for exact replay.

    The key is a hash of the model name and the full prompt, so a replay is only
    served when the request is byte-identical to the recorded one. A near-miss
    raises rather than silently returning a different day's plan.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, RecordedTrace] | None = None

    @staticmethod
    def key_for(model: str, prompt: str) -> str:
        return hashlib.sha256(f"{model}\x00{prompt}".encode()).hexdigest()[:32]

    def _load(self) -> dict[str, RecordedTrace]:
        if self._index is None:
            index: dict[str, RecordedTrace] = {}
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        index[json.loads(line)["key"]] = RecordedTrace(**json.loads(line))
            self._index = index
        return self._index

    def get(self, key: str) -> RecordedTrace | None:
        return self._load().get(key)

    def append(self, trace: RecordedTrace) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace.__dict__, sort_keys=True) + "\n")
        self._load()[trace.key] = trace

    def __len__(self) -> int:
        return len(self._load())


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model response that may be wrapped in prose."""
    fenced = text.split("```")
    for chunk in fenced:
        candidate = chunk.removeprefix("json").strip()
        if candidate.startswith("{"):
            return candidate
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


@dataclass
class LlmPlanner:
    """A planner backed by a language model, or by a recorded trace of one.

    Attributes:
        model: Model identifier, passed to ``call_fn``. Switching model is a
            configuration change and nothing else (R11).
        call_fn: ``(model, system, prompt) -> str``. Left None, the planner is
            replay-only: it serves recorded traces and raises if one is missing.
            Injecting the transport this way is what keeps the test suite, and the
            reviewer reproducing a figure, entirely offline.
        traces: Where calls are recorded and replayed from.
        record: Whether to append new calls to the store.
    """

    model: str = "claude-opus-5"
    call_fn: Callable[[str, str, str], str] | None = None
    traces: TraceStore | None = None
    record: bool = True
    name: str = "llm"

    def __post_init__(self) -> None:
        if self.name == "llm":
            self.name = f"llm:{self.model}"

    def build_prompt(self, context: PlanningContext) -> str:
        hub = context.hub
        lines = [
            f"Date: {context.date}",
            f"Greenhouse floor area: {hub.floor_area_m2:,.0f} m2",
            f"Grid contract: import {hub.contract.import_limit_kw:,.0f} kW, "
            f"export {hub.contract.export_limit_kw:,.0f} kW",
        ]
        if hub.contract.congestion_windows:
            windows = ", ".join(
                f"hour {h}: import {i:,.0f} kW / export {e:,.0f} kW"
                for h, (i, e) in sorted(hub.contract.congestion_windows.items())
            )
            lines.append(f"Congestion windows -- reduced limits apply: {windows}")
        lines += [
            f"Battery: {hub.battery.capacity_kwh:,.0f} kWh, "
            f"max {hub.battery.max_charge_kw:,.0f} kW charge / "
            f"{hub.battery.max_discharge_kw:,.0f} kW discharge, "
            f"usable {hub.battery.soc_min_kwh:,.0f}-{hub.battery.soc_max_kwh:,.0f} kWh, "
            f"starting at {hub.battery.soc_init_kwh:,.0f} kWh",
            f"CHP: {hub.chp.electrical_capacity_kw:,.0f} kWe, "
            f"heat/power ratio {hub.chp.heat_to_power_ratio}, "
            f"min run {hub.chp.min_run_hours} h, min down {hub.chp.min_down_hours} h",
            f"Boiler: {hub.boiler.thermal_capacity_kw:,.0f} kWth",
            f"Heat buffer: {hub.buffer.capacity_kwh:,.0f} kWh, "
            f"starting at {hub.buffer.level_init_kwh:,.0f} kWh",
            f"Lamps: {hub.lamp_capacity_kw:,.0f} kW at full power",
            f"Crop needs a supplemental daily light integral of "
            f"{hub.crop.dli_target_mol_m2} +/- {hub.crop.dli_tolerance_mol_m2} mol/m2; "
            f"one hour at full lamp power gives {hub.hourly_dli_mol_m2(1.0):.2f} mol/m2",
            "",
            "Hourly forecast (this is a forecast, not what will actually happen):",
            "hour | power EUR/kWh | gas EUR/kWh | heat demand kW | irradiance W/m2",
        ]
        for c in context.forecast:
            lines.append(
                f"{c.hour:4d} | {c.power_price_eur_kwh:13.4f} | {c.gas_price_eur_kwh:11.4f} "
                f"| {c.heat_demand_kw:14.0f} | {c.irradiance_w_m2:15.0f}"
            )
        if context.brief:
            lines += ["", f"Operator brief: {context.brief}"]
        if context.previous_verdict is not None and not context.previous_verdict.accepted:
            lines += [
                "",
                f"Your previous plan (revision {context.revision - 1}) was rejected.",
                context.previous_verdict.feedback(explain=True),
                "Revise it. Change only what is needed to clear these violations.",
            ]
        return "\n".join(lines)

    def plan(self, context: PlanningContext) -> Plan:
        """Produce a plan, replaying a recorded call when one matches.

        Raises:
            IntentSchemaError: if the model returns something that is not a plan.
            RuntimeError: in replay-only mode with no matching recorded trace.
        """
        prompt = self.build_prompt(context)
        key = TraceStore.key_for(self.model, prompt)

        # `is not None`, not truthiness: TraceStore defines __len__, so an empty
        # store is falsy and a plain `if self.traces` would silently skip recording
        # on the very first call -- which is exactly the call that needs recording.
        trace = self.traces.get(key) if self.traces is not None else None
        if trace is not None:
            response = trace.response
        elif self.call_fn is None:
            raise RuntimeError(
                f"no recorded trace for model {self.model!r} at revision "
                f"{context.revision} on {context.date}, and this planner has no "
                f"call_fn, so it cannot reach a live model. Either supply call_fn "
                f"to record a fresh run, or point `traces` at a store that contains "
                f"this run. Replay requires the prompt to match exactly."
            )
        else:
            response = self.call_fn(self.model, SYSTEM_PROMPT, prompt)
            if self.traces is not None and self.record:
                self.traces.append(
                    RecordedTrace(
                        key=key,
                        model=self.model,
                        prompt=prompt,
                        response=response,
                        revision=context.revision,
                        date=context.date,
                    )
                )

        payload = _extract_json(response)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise IntentSchemaError(
                f"model {self.model!r} did not return parseable JSON: {exc}"
            ) from exc

        data.setdefault("date", context.date)
        data["planner"] = self.name
        data["revision"] = context.revision
        data["brief"] = context.brief
        if isinstance(data.get("intervals"), list) and len(data["intervals"]) != HOURS_PER_DAY:
            raise IntentSchemaError(
                f"model {self.model!r} returned {len(data['intervals'])} intervals, "
                f"expected {HOURS_PER_DAY}"
            )
        return Plan.from_dict(data)


def anthropic_call_fn(api_key: str | None = None, max_tokens: int = 8000):
    """Build a ``call_fn`` backed by the Anthropic API.

    Import is deferred so ``kasflex`` installs and runs without the ``llm`` extra.
    """

    def call(model: str, system: str, prompt: str) -> str:
        from anthropic import Anthropic  # noqa: PLC0415

        client = Anthropic(api_key=api_key) if api_key else Anthropic()
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    return call
