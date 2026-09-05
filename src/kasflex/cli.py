"""Command line interface.

Five commands, each of which does one thing:

    kasflex run         one scenario, printed as a plan the operator can read
    kasflex experiment  the full matrix, unattended, writing structured records
    kasflex verify      check a plan file against a scenario's limits
    kasflex datasets    the data provenance registry
    kasflex doctor      what is installed and what is missing

``run`` and ``experiment`` work offline on a bare clone with no API key and no
downloads, which is acceptance criteria 2 and 7.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kasflex.checker.rules import CheckerConfig, SafetyChecker
from kasflex.config import ScenarioConfig
from kasflex.data.registry import DATASETS
from kasflex.data.synthetic import synthetic_day
from kasflex.experiment import (
    ExperimentMatrix,
    build_greenhouse,
    build_planner,
    render_summary,
    summarise,
)
from kasflex.intent import IntentSchemaError, Plan
from kasflex.oversight import AuditLog
from kasflex.run import run_scenario

DEFAULT_CONFIG = "configs/scenario_westland_winter.yaml"


def _load_day(config: ScenarioConfig, seed: int | None = None):
    """Load the scenario's conditions. Synthetic today; cached series in stage 3."""
    if config.data_source == "cache":
        raise SystemExit(
            "data_source: cache is not wired up yet. The cache and its provenance "
            "manifest exist (see kasflex.data.cache), but the ENTSO-E and Open-Meteo "
            "fetchers land in stage 3 of the MVP plan. Use data_source: synthetic."
        )
    return synthetic_day(
        config.date,
        seed=config.seed if seed is None else seed,
        floor_area_m2=config.hub.floor_area_m2,
        winter=config.winter,
    )


def _print_plan(plan: Plan, limit: int = 24) -> None:
    print(f"\nPlan for {plan.date} by {plan.planner} (revision {plan.revision})")
    print(f"{'hr':>3}  {'heat':<7} {'light':>6}  {'battery':<10} {'CHP':<11} {'CO2':<7} reasoning")
    print("-" * 100)
    for iv in plan.intervals[:limit]:
        power = f"{iv.battery_power_kw:.0f}kW" if iv.battery != "idle" else ""
        print(
            f"{iv.hour:3d}  {iv.heat_source:<7} {iv.lighting_level:6.2f}  "
            f"{iv.battery + ' ' + power:<10} {iv.chp_mode:<11} {iv.co2_source:<7} "
            f"{iv.reasoning[:38]}"
        )


def cmd_run(args: argparse.Namespace) -> int:
    config = ScenarioConfig.from_yaml(args.config)
    if args.planner:
        config = ScenarioConfig(**{**config.__dict__, "planner": args.planner})
    if args.greenhouse:
        config = ScenarioConfig(**{**config.__dict__, "greenhouse": args.greenhouse})
    if args.no_checker:
        config = ScenarioConfig(
            **{**config.__dict__, "checker": CheckerConfig(**{**config.checker.__dict__,
                                                             "enabled": False})}
        )

    day = _load_day(config)
    result = run_scenario(
        scenario=config.name,
        date=config.date,
        hub=config.hub,
        forecast=day.forecast,
        actual=day.actual,
        planner=build_planner(config.planner, config),
        greenhouse=build_greenhouse(config.greenhouse, config),
        checker_config=config.checker,
        audit_log=AuditLog(config.audit_path),
        brief=config.brief,
        seed=config.seed,
        provenance={"data_source": config.data_source},
    )

    if not args.quiet:
        _print_plan(result.plan)
        print(f"\nChecker: {'enabled' if result.checker_enabled else 'DISABLED'}")
        print(result.verdict.feedback(explain=config.checker.explain, limit=8))
        if result.fell_back_to_baseline:
            print("\nControl was handed to the rule-based baseline.")
        print("\nRealised day:")
        for key, value in result.metrics.items():
            print(f"  {key:<28} {value:>12,.2f}")
        # Hard and projected are reported separately, always. A bare count mixes an
        # arithmetic contract breach with a forward model's guess about humidity,
        # and the two mean very different things (docs/DECISIONS.md ADR-0007).
        hard = result.realised_hard_violations
        projected = len(result.realised_violations) - hard
        print("\n  Audited against the realised day with every check enabled:")
        print(f"    hard violations       {hard:>6}   (limits breached, decided exactly)")
        print(
            f"    projected violations  {projected:>6}"
            "   (climate bands, from the model's projection)"
        )
        if projected:
            kinds = sorted({v["constraint"] for v in result.realised_violations
                            if v["severity"] == "projected"})
            print(f"    projected constraints: {', '.join(kinds)}")
        if not result.outcome.validated:
            print(
                f"\n  NOTE: the '{result.greenhouse_model}' greenhouse model has not been "
                f"validated against measured data. These figures are not operational advice."
            )

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(result.to_record(), indent=2, default=str))
        print(f"\nWrote {args.json_out}")
    return 0


def cmd_experiment(args: argparse.Namespace) -> int:
    config = ScenarioConfig.from_yaml(args.config)
    matrix = ExperimentMatrix(config=config, days=args.days, output_path=args.output)
    print(f"Running {len(matrix.conditions)} conditions x {args.days} days "
          f"= {len(matrix.conditions) * args.days} runs\n")
    records = matrix.run()
    print(render_summary(summarise(records)))
    print(f"\nWrote {args.output} ({len(records)} records)")
    print(f"Audit log: {config.audit_path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    config = ScenarioConfig.from_yaml(args.config)
    try:
        plan = Plan.from_json(Path(args.plan).read_text())
    except IntentSchemaError as exc:
        print(f"Not a valid plan: {exc}", file=sys.stderr)
        return 2

    day = _load_day(config)
    greenhouse = build_greenhouse(config.greenhouse, config)
    outcome = greenhouse.simulate_day(plan, day.forecast, config.hub.floor_area_m2)
    import dataclasses

    conditions = tuple(
        dataclasses.replace(
            c, heat_demand_kw=outcome.heat_demand_kw[i], co2_demand_kg_h=outcome.co2_demand_kg_h[i]
        )
        for i, c in enumerate(day.forecast)
    )
    verdict = SafetyChecker(config.hub, config.checker).verify(
        plan, conditions, outcome.projection()
    )
    print(verdict.feedback(explain=config.checker.explain, limit=100))
    if args.json_out:
        Path(args.json_out).write_text(verdict.to_json())
    return 0 if verdict.accepted else 1


def cmd_datasets(args: argparse.Namespace) -> int:
    if args.markdown:
        from kasflex.data.registry import as_markdown_table

        print(as_markdown_table())
        return 0
    for ref in DATASETS.values():
        print(f"\n{ref.key}  [{ref.phase}]  {ref.title}")
        print(f"  kind     {ref.kind}")
        print(f"  source   {ref.source}")
        print(f"  licence  {ref.licence}")
        print(f"  access   {ref.access}")
        print(f"  checked  {ref.verified_on}")
        if ref.notes:
            print(f"  notes    {ref.notes}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print("KasFlex environment check\n")
    import importlib.util

    def status(module: str, purpose: str, extra: str) -> None:
        found = importlib.util.find_spec(module) is not None
        print(f"  [{'x' if found else ' '}] {module:<22} {purpose:<34} {'' if found else extra}")

    print("Core:")
    status("numpy", "arrays", "pip install kasflex")
    status("yaml", "scenario configuration", "pip install kasflex")
    print("\nOptional:")
    status("power_grid_model", "grid power flow (phase 2)", "pip install 'kasflex[grid]'")
    status("pandas", "data acquisition", "pip install 'kasflex[data]'")
    status("pyarrow", "parquet cache", "pip install 'kasflex[data]'")
    status("entsoe", "ENTSO-E prices", "pip install 'kasflex[data]'")
    status("anthropic", "language-model planner", "pip install 'kasflex[llm]'")

    print("\nGreenLight worker (separate environment on purpose):")
    from kasflex.adapters.greenlight_worker import GreenLightWorker

    worker = GreenLightWorker()
    if worker.available():
        print(f"  [x] interpreter          {worker.python}")
    else:
        print(f"  [ ] interpreter          {worker.python} not found")
        print("      python3 -m venv .venv-greenlight")
        print("      ./.venv-greenlight/bin/pip install -r workers/greenlight/requirements.txt")

    if importlib.util.find_spec("gl_gym") is not None:
        print(
            "\n  WARNING: gl_gym is importable from THIS environment. It is AGPL-3.0 "
            "and pins numpy<2.\n"
            "  KasFlex core never imports it, but installing it here can hold "
            "power-grid-model\n  back to an old release. Keep it in the worker "
            "environment. See docs/DECISIONS.md ADR-0002."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kasflex",
        description="Verified agentic energy management for greenhouse horticulture.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one scenario")
    p_run.add_argument("--config", default=DEFAULT_CONFIG)
    p_run.add_argument("--planner",
                       choices=["rule-based", "naive", "learned", "llm", "mpc"])
    p_run.add_argument("--greenhouse", choices=["surrogate", "greenlight"])
    p_run.add_argument("--no-checker", action="store_true",
                       help="run unverified (the 'checker disabled' arm)")
    p_run.add_argument("--json-out")
    p_run.add_argument("--quiet", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_exp = sub.add_parser("experiment", help="run the full matrix unattended")
    p_exp.add_argument("--config", default=DEFAULT_CONFIG)
    p_exp.add_argument("--days", type=int, default=3)
    p_exp.add_argument("--output", default="results/runs.jsonl")
    p_exp.set_defaults(func=cmd_experiment)

    p_ver = sub.add_parser("verify", help="verify a plan file")
    p_ver.add_argument("--config", default=DEFAULT_CONFIG)
    p_ver.add_argument("--plan", required=True)
    p_ver.add_argument("--json-out")
    p_ver.set_defaults(func=cmd_verify)

    p_data = sub.add_parser("datasets", help="show the data provenance registry")
    p_data.add_argument("--markdown", action="store_true")
    p_data.set_defaults(func=cmd_datasets)

    p_doc = sub.add_parser("doctor", help="check the environment")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
