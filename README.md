# KasFlex

**A planner proposes a day of greenhouse energy use. A deterministic checker tests
it. A person records the decision.**

Many Dutch greenhouses combine flexible assets — batteries, CHP units, heat
buffers and controllable lighting — that may be useful under grid constraints. A
planner could propose when to use them.
But a proposal should not reach equipment merely because a planner produced it.

KasFlex prototypes a visible planning, verification and fallback boundary, then
measures how the experimental arms behave on a simulation.

> **Simulation only.** No greenhouse equipment is connected. Nothing here is
> validated for operational use, and no figure produced with the built-in surrogate
> greenhouse model may be published as a result.

4TU.NIRICT · WUR · TU Delft · TU/e · UT

---

## Try it

From source:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
kasflex experiment --days 3
```

After installation, this command needs no API key or network: it uses deterministic
local fixtures. The core four-arm excerpt from a representative three-day run of
this revision is:

```
condition               runs    cost EUR  violations   hard  fallback   peak kW
-------------------------------------------------------------------------------
rule-based                 3    11561.62        0.00      0      0.00      5718
learned                    3    10716.28        0.33      1      0.00      5650
ai-unverified              3    15035.00       35.00    105      0.00      6631
ai-verified                3    11561.62        0.00      0      1.00      5718
```

The full matrix also exercises learned-planner and verification ablations. Its MPC
rows deliberately report `MpcNotImplementedError`: the reference controller is a
remaining research blocker, not a silently substituted implementation.

Read it two rows at a time:

- **`ai-unverified`** — a deliberately poor, constraint-blind negative-control
  fixture with the checker off. It averages 35 realised hard violations and costs
  more than the baseline; it is not a result from a language model.
- **`ai-verified`** — the same fixture with the checker on. Its unsafe proposal is
  rejected and execution falls back to the conventional baseline. Violations: zero.
- **`learned`** — forecasts tomorrow's heat demand from past operation, then
  searches for a lower-cost schedule. In this small surrogate run it costs about
  7% less than the baseline but has one realised hard violation across three days.

Those numbers are **apparatus, not findings**: the greenhouse model is not yet
validated. They show the measurement works, which is what this stage owes.

## The interface

```bash
kasflex ui
```

The **KasFlex Decision Cockpit** keeps the simulated status, scale, data source,
condition and plan fingerprint visible. Change the scenario, generate a plan, edit
any hour, and record approval or rejection against that exact preview. An edited
plan cannot be approved until it has been re-verified. Approval is an experiment
record; it does not actuate equipment. With the checker switched off the verdict
reads *not verified*, never *accepted*.

## Run it every day

```bash
export ENTSOE_API_KEY=...
kasflex daily
```

This experimental path attempts to populate a local price/weather cache and append
a daily record; a network connection and source credentials can be needed on a
cache miss. Its live request contracts and its integration with the main browser
and experiment paths still require verification. See [deploy/](deploy/README.md)
for the deployment scaffold.

## Documentation

| | |
|---|---|
| [MVP plan](docs/MVP_PLAN.md) | Build order, requirement coverage, risks |
| [Architecture](docs/ARCHITECTURE.md) | How the pieces fit, and why |
| [Decisions](docs/DECISIONS.md) | Why things are the way they are |
| [Usage](docs/USAGE.md) | Every command |
| [Data](docs/DATA.md) | Datasets, DOIs, licences, provenance |
| [FAIR](docs/FAIR.md) | FAIR assessment, including the gaps |
| [Packaging](packaging/README.md) | Building the double-clickable application |

KasFlex does not claim validated greenhouse physics or feeder power flow. It has
integration boundaries for
[GreenLight-Gym2](https://github.com/BartvLaatum/GreenLight-Gym2) and
[power-grid-model](https://github.com/PowerGridModel/power-grid-model), and adds a
surrogate greenhouse, an energy-hub simulation, a safety checker, recorded human
review and an experiment harness. Those external integrations are not yet the
validated default path.

**One thing to know before you extend it:** GreenLight-Gym2 is AGPL-licensed and
pins an incompatible NumPy version, so it runs in a separate process under its own
virtual environment. `kasflex doctor` warns if you break that. See
[ADR-0001 and ADR-0002](docs/DECISIONS.md).

## Status

The research apparatus runs end to end and entirely offline on synthetic fixtures.
Dataset acquisition and caching exist, but cached real data are not yet wired into
the main browser and experiment paths. The safety checker, energy hub, experiment
harness, browser interface, and baseline, fixture and learned planning paths are
implemented. GreenLight-Gym2 validation against measured data, the MPC reference,
live-model trials, live-source verification and unfamiliar-user testing remain
incomplete. Current numbers demonstrate the experiment mechanism, not scientific
or operational performance.
See the candid [remake review](docs/REMAKE_REVIEW.md) for source assessment,
non-claims, research blockers and the prioritized roadmap.

## Licence

[Apache-2.0](LICENSE). `workers/greenlight/worker.py` is AGPL-3.0-or-later,
inherited from `gl-gym` and isolated to that one file.
