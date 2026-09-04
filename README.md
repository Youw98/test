# KasFlex

**Verified agentic energy management for greenhouse horticulture under electrical
constraints.**

An AI planner proposes one day of hourly energy intent for a greenhouse energy hub.
A deterministic safety checker verifies it against electrical, asset and crop limits.
A person approves it. The day is then simulated and compared against conventional
control.

The system exists to produce one measurement:

| | Checker disabled | Checker enabled |
|---|---|---|
| Operating cost | — | — |
| Limit violations | — | — |

> **Simulation only.** No physical greenhouse equipment is connected at any point.
> Nothing here is validated for operational use, and no figure produced with the
> built-in surrogate greenhouse model may be published as a result.

4TU.NIRICT · WUR · TU Delft · TU/e · UT

---

## Try it in two minutes

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
kasflex experiment --days 3
```

No API key, no downloads, no network. That command runs six experimental conditions
across three days and prints:

```
condition               runs    cost EUR  violations   hard  fallback   peak kW
-------------------------------------------------------------------------------
rule-based                 3    10498.15        0.00      0      0.00      5718
ai-unverified              3    11601.63       66.00    198      0.00     10650
ai-verified                3    10498.15        0.00      0      1.00      5718
ai-verified-silent         3    10498.15        0.00      0      1.00      5718
ai-verified-gap            3    10498.15        0.00      0      1.00      5718
```

An unverified constraint-blind planner racks up 66 violations and costs *more* than
the baseline. Verified, the same planner is rejected until control falls back to the
baseline, and violations go to zero.

Those numbers are **apparatus, not findings** — the greenhouse model is an
unvalidated surrogate and the planner is a fixture. They demonstrate that the
measurement works, which is what the MVP owes at this stage. See the
[MVP plan](docs/MVP_PLAN.md).

## Architecture

![Architecture](docs/architecture.png)

Green is KasFlex. Blue is someone else's validated code or data. Orange is the
person. Grey is phase 2. Full description in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

KasFlex does not implement greenhouse physics or power flow. It wraps
[GreenLight-Gym2](https://github.com/BartvLaatum/GreenLight-Gym2) and
[power-grid-model](https://github.com/PowerGridModel/power-grid-model), and adds
what neither has: market prices, the energy hub, the intent abstraction, the safety
checker, human approval, and the experiment harness.

## The three ideas

**Planners emit intent, not actuator values.** Heat source, lighting level, battery
direction, CHP mode, CO2 source — one record per hour. A deterministic low-level
controller turns accepted intent into 15-minute setpoints. The rule-based baseline
and the MPC reference emit the *same* structure, so the experiment measures the
planner rather than the low-level controller.

**The checker sits between the planner and everything downstream.** Not beside it.
No intent reaches an actuator without a verdict from pure, deterministic code that
has no dependency on the planner. Rejections are machine-readable and name the
constraint, the interval, the actual value and the feasible bound — enough for the
planner to fix the plan rather than resample it.

**Dispatch never repairs a bad plan.** An infeasible request is carried out as
written and the out-of-bounds state is recorded. Clipping it would quietly fix bad
plans and the "checker disabled" column would read zero for the wrong reason.

## Documentation

| | |
|---|---|
| [MVP plan](docs/MVP_PLAN.md) | Build order, requirement coverage, acceptance criteria, risks |
| [Architecture](docs/ARCHITECTURE.md) | The seams and why each is where it is |
| [Decisions](docs/DECISIONS.md) | ADRs, including the AGPL and numpy findings |
| [Usage](docs/USAGE.md) | Every command, and how to extend it |
| [Data](docs/DATA.md) | Datasets, DOIs, licences, provenance |
| [FAIR](docs/FAIR.md) | FAIR assessment, including the gaps |

## Two things to know before you extend it

**`gl-gym` is AGPL-3.0-or-later and pins `numpy<2`; `power-grid-model` needs
`numpy>=2`.** KasFlex core is Apache-2.0 and never imports `gl_gym` — the greenhouse
model runs in a separate process under its own virtual environment. This is
load-bearing for two independent reasons, and `kasflex doctor` will warn you if you
break it. See [ADR-0001 and ADR-0002](docs/DECISIONS.md).

**Validate at 96 m², run scenarios at 5 ha, never mix them.** The scales differ by
a factor of roughly 400. Every energy figure is reported per square metre alongside
its absolute value. See [ADR-0004](docs/DECISIONS.md).

## Status

Stages 0 and 2 built and tested. 107 tests, 94% coverage, offline.

| Stage | | |
|---|---|---|
| 0 | Skeleton running end to end | ✅ |
| 1 | GreenLight-Gym2, validated against AGC measurements | ⬜ next |
| 2 | Safety checker | ✅ |
| 3 | Real ENTSO-E prices and Dutch weather | 🔶 |
| 4 | Language-model planner | 🔶 built, needs live models |
| 5 | MPC reference | ⬜ |
| 6 | Browser interface | ⬜ |
| 7 | Fleet and grid | 🔶 adapter built |

## Citing

See [`CITATION.cff`](CITATION.cff). If you use KasFlex, please also cite
GreenLight-Gym2 ([10.1016/j.ifacol.2025.11.827](https://doi.org/10.1016/j.ifacol.2025.11.827))
and, where relevant, the Autonomous Greenhouse Challenge dataset
([10.4121/uuid:88d22c60-21b3-4ea8-90db-20249a5be2a7](https://doi.org/10.4121/uuid:88d22c60-21b3-4ea8-90db-20249a5be2a7)).

## Licence

[Apache-2.0](LICENSE) for the core. `workers/greenlight/worker.py` is
AGPL-3.0-or-later, inherited from `gl-gym` and isolated to that one file.
