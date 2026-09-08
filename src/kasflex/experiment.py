"""Unattended experiment matrix (R32, R33).

Runs every combination of planner, checker condition and day, and writes one
structured JSON record per run. The whole matrix is driven from the command line
and needs no interaction, which is what lets acceptance criterion 6 be met and
criterion 7 -- an external researcher regenerating every figure with one command --
be built on top of it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kasflex.adapters.greenhouse import GreenhouseModel, SurrogateGreenhouse
from kasflex.checker.rules import CheckerConfig
from kasflex.config import ScenarioConfig
from kasflex.controllers.base import Planner
from kasflex.controllers.naive import NaivePlanner
from kasflex.controllers.rule_based import RuleBasedPlanner
from kasflex.data.synthetic import synthetic_day
from kasflex.oversight import AuditLog
from kasflex.resources import resolve_output
from kasflex.run import RunResult, run_scenario


@dataclass(frozen=True)
class Condition:
    """One cell of the matrix: a planner paired with a checker configuration."""

    label: str
    planner: str
    checker: CheckerConfig


def default_conditions() -> list[Condition]:
    """The four arms acceptance criterion 4 requires, plus the R21 exclusion arm.

    ``learned`` is the data-driven planner: a demand model fitted to past operation,
    feeding a scheduler that optimises against the real dispatch model. Pairing it
    with ``learned-unverified`` asks the headline question of a planner that is
    actually trying to do well, rather than only of a fixture built to fail.

    ``ai-unverified`` versus ``ai-verified`` is the headline comparison.
    ``ai-verified-silent`` separates the value of verification from the value of
    explanation (R19). ``ai-verified-gap`` leaves one designated check off so that
    unchecked degradation can be measured (R21).
    """
    return [
        Condition("rule-based", "rule-based", CheckerConfig(enabled=True)),
        Condition("learned", "learned", CheckerConfig(enabled=True)),
        Condition("learned-unverified", "learned", CheckerConfig(enabled=False)),
        Condition("mpc", "mpc", CheckerConfig(enabled=True)),
        Condition("ai-unverified", "naive", CheckerConfig(enabled=False)),
        Condition("ai-verified", "naive", CheckerConfig(enabled=True)),
        Condition("ai-verified-silent", "naive", CheckerConfig(enabled=True, explain=False)),
        Condition(
            "ai-verified-gap",
            "naive",
            CheckerConfig(enabled=True, excluded_checks=("grid.import_limit",)),
        ),
    ]


def build_planner(name: str, config: ScenarioConfig) -> Planner:
    """Instantiate a planner by name.

    Imports for the language-model and MPC planners are deferred so that the matrix
    runs offline for the planners that do not need them.
    """
    if name == "rule-based":
        return RuleBasedPlanner()
    if name == "naive":
        return NaivePlanner()
    if name == "mpc":
        from kasflex.controllers.mpc import MpcPlanner  # noqa: PLC0415

        return MpcPlanner()
    if name == "learned":
        from datetime import date as Date  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415

        from kasflex.controllers.scheduler import LearnedPlanner  # noqa: PLC0415
        from kasflex.data.synthetic import synthetic_history  # noqa: PLC0415
        from kasflex.forecast.history import build_history  # noqa: PLC0415

        # The history is the greenhouse's own past demand, produced by the same
        # model the run will execute. Training on anything else forecasts a
        # different greenhouse -- see kasflex.forecast.history.
        # Training days must end before the target. The old fixed 2023-01-01 start
        # leaked future synthetic days into early-January scenarios.
        history_start = Date.fromisoformat(config.date) - timedelta(days=config.history_days)
        weather = synthetic_history(
            config.history_days,
            seed=config.seed + 9_000,
            start_date=history_start.isoformat(),
            floor_area_m2=config.hub.floor_area_m2,
            winter=config.winter,
        )
        history = build_history(
            weather.days, build_greenhouse(config.greenhouse, config), config.hub.floor_area_m2
        )
        return LearnedPlanner(history=history)
    if name == "llm":
        from kasflex.controllers.llm import LlmPlanner, TraceStore  # noqa: PLC0415

        return LlmPlanner(
            model=config.llm_model,
            traces=TraceStore(resolve_output(config.trace_path)),
        )
    raise ValueError(f"unknown planner {name!r}; available: rule-based, naive, learned, mpc, llm")


def build_greenhouse(name: str, config: ScenarioConfig) -> GreenhouseModel:
    if name == "surrogate":
        return SurrogateGreenhouse()
    if name == "greenlight":
        from kasflex.adapters.greenlight_worker import GreenLightWorker  # noqa: PLC0415

        return GreenLightWorker(scenario=config.greenlight_scenario, seed=config.seed)
    raise ValueError(f"unknown greenhouse model {name!r}; available: surrogate, greenlight")


@dataclass
class ExperimentMatrix:
    """A matrix of conditions crossed with days."""

    config: ScenarioConfig
    conditions: list[Condition] = field(default_factory=default_conditions)
    days: int = 3
    output_path: str = "results/runs.jsonl"
    continue_on_error: bool = True
    """Keep going when one cell fails. A whole matrix should not be lost because one
    planner is not implemented yet -- the failure is recorded as its own record."""

    def cells(self) -> Iterator[tuple[Condition, int]]:
        for condition in self.conditions:
            for day in range(self.days):
                yield condition, day

    def run(self, verbose: bool = True) -> list[dict[str, Any]]:
        """Execute every cell and write one JSON record per run."""
        out = Path(self.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        audit = AuditLog(resolve_output(self.config.audit_path))
        records: list[dict[str, Any]] = []

        with out.open("w", encoding="utf-8") as fh:
            for condition, day_index in self.cells():
                seed = self.config.seed + day_index
                day = synthetic_day(
                    self.config.date,
                    seed=seed,
                    floor_area_m2=self.config.hub.floor_area_m2,
                    winter=self.config.winter,
                )
                try:
                    result: RunResult = run_scenario(
                        scenario=f"{self.config.name}/{condition.label}",
                        date=self.config.date,
                        hub=self.config.hub,
                        forecast=day.forecast,
                        actual=day.actual,
                        planner=build_planner(condition.planner, self.config),
                        greenhouse=build_greenhouse(self.config.greenhouse, self.config),
                        checker_config=condition.checker,
                        audit_log=audit,
                        brief=self.config.brief,
                        seed=seed,
                        provenance={
                            "data_source": self.config.data_source,
                            "condition": condition.label,
                            "day_index": day_index,
                        },
                    )
                    record = result.to_record()
                    record["condition"] = condition.label
                    record["day_index"] = day_index
                    record["status"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    if not self.continue_on_error:
                        raise
                    record = {
                        "scenario": f"{self.config.name}/{condition.label}",
                        "condition": condition.label,
                        "day_index": day_index,
                        "seed": seed,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                records.append(record)
                if verbose:
                    _print_row(record)
        return records


def _print_row(record: dict[str, Any]) -> None:
    if record.get("status") == "error":
        print(
            f"  {record['condition']:<20} day {record['day_index']}  ERROR  {record['error'][:70]}"
        )
        return
    print(
        f"  {record['condition']:<20} day {record['day_index']}  "
        f"cost EUR {record.get('net_cost_eur', 0):>9.2f}  "
        f"violations {record.get('realised_violations_total', 0):>3}  "
        f"peak {record.get('peak_import_kw', 0):>6.0f} kW  "
        f"{'fallback' if record.get('fell_back_to_baseline') else ''}"
    )


def summarise(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Aggregate the matrix into the headline table: cost and violations per arm."""
    by_condition: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("status") != "ok":
            continue
        by_condition.setdefault(r["condition"], []).append(r)

    summary: dict[str, dict[str, float]] = {}
    for label, rows in by_condition.items():
        n = len(rows)
        summary[label] = {
            "runs": float(n),
            "mean_cost_eur": round(sum(r["net_cost_eur"] for r in rows) / n, 2),
            "mean_violations": round(sum(r["realised_violations_total"] for r in rows) / n, 2),
            "hard_violations": float(sum(r["realised_violations_hard"] for r in rows)),
            "fallback_rate": round(sum(1 for r in rows if r["fell_back_to_baseline"]) / n, 3),
            "mean_peak_import_kw": round(sum(r["peak_import_kw"] for r in rows) / n, 1),
        }
    return summary


def render_summary(summary: dict[str, dict[str, float]]) -> str:
    """The headline table from the requirements, section 1."""
    lines = [
        "",
        f"{'condition':<22}{'runs':>6}{'cost EUR':>12}{'violations':>12}"
        f"{'hard':>7}{'fallback':>10}{'peak kW':>10}",
        "-" * 79,
    ]
    for label, s in summary.items():
        lines.append(
            f"{label:<22}{s['runs']:>6.0f}{s['mean_cost_eur']:>12.2f}"
            f"{s['mean_violations']:>12.2f}{s['hard_violations']:>7.0f}"
            f"{s['fallback_rate']:>10.2f}{s['mean_peak_import_kw']:>10.0f}"
        )
    return "\n".join(lines)
