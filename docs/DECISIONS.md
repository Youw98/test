# Design decisions

Each entry states the decision, what forced it, and what it costs. Several of these
were settled by checking rather than by reasoning; where that happened, the check is
recorded so it can be repeated when the upstream projects move.

All external facts below were verified on **2026-09-04**. Re-verify at project
start: the requirements document is right that repository locations change —
`power-grid-model` has already moved out of the Alliander organisation into its own.

---

## ADR-0001 — GreenLight-Gym2 runs in a separate process, and KasFlex core never imports it

**Decision.** `kasflex` core is Apache-2.0 and imports nothing from `gl_gym`. The
greenhouse model is driven through `workers/greenlight/worker.py`, a JSON-line
program run under its own interpreter. That file is AGPL-3.0-or-later and imports
nothing from KasFlex.

**What forced it.** `gl-gym` 0.3.2 declares `License-Expression: AGPL-3.0-or-later`.
AGPL's copyleft reaches any work linked into it, and its network clause reaches
software offered to users over a network — which the browser interface (R27) is.
Importing `gl_gym` from KasFlex core would therefore make the whole of KasFlex
AGPL, including for any partner who reuses it.

```console
$ python -c "import importlib.metadata as m; print(m.metadata('gl-gym')['License-Expression'])"
AGPL-3.0-or-later
```

**Why this arrangement is sound.** Apache-2.0 is one-way compatible with GPLv3 and
AGPLv3, so Apache-licensed code may be combined into an AGPL work. The worker is
distributed as AGPL and may freely be combined with `gl_gym`; the core, distributed
on its own and importing nothing copyleft, stays Apache-2.0. The copyleft surface is
exactly one file plus its own virtual environment.

**Cost.** A subprocess boundary and a JSON round trip per simulated day. Measured at
roughly 0.1 s for a 24-hour simulation, against a process start of a few hundred
milliseconds, so the boundary rather than the physics dominates — and both are
negligible next to a language-model call.

**Do not.** Do not "simplify" this by importing `gl_gym` into core. It is the single
change that would most damage the project's reusability, and it would not announce
itself: everything would keep working.

**Open item.** Ask Bart van Laatum (WUR) whether a non-AGPL licence exception is
available for `gl-gym`. If it is, this ADR can be revisited and the boundary
simplified. Until then it stands.

---

## ADR-0002 — The grid extra and the GreenLight worker never share an environment

**Decision.** `pip install 'kasflex[grid]'` (power flow, `numpy>=2`) goes in the main
environment. `gl-gym` (`numpy<2`) goes in `.venv-greenlight`. Core itself depends on
`numpy>=1.26` and avoids numpy-2-only APIs, so it installs correctly in both.

**What forced it.** Current releases pin incompatible numpy majors:

| Package | Version | numpy requirement | Licence |
|---|---|---|---|
| `gl-gym` | 0.3.2 | `numpy<2.0` | AGPL-3.0-or-later |
| `power-grid-model` | 1.12.110 | `numpy>=2.0.0` | MPL-2.0 |
| `power-grid-model-ds` | 1.5.3 | `numpy>=2.0` | MPL-2.0 |

**A correction worth recording.** These two *can* be co-installed — but only because
pip silently backtracks `power-grid-model` to **1.12.43**, an old release whose pin
permits `numpy<2`. Asking for a current version alongside `gl-gym` breaks the pin:

```console
$ pip install gl-gym==0.3.2 power-grid-model
Successfully installed gl-gym-0.3.2 numpy-1.26.4 power-grid-model-1.12.43   # silent downgrade

$ pip install "power-grid-model>=1.12.100"
ERROR: ... gl-gym 0.3.2 requires numpy<2.0, but you have numpy 2.4.6 which is incompatible.
```

So the honest statement is not "impossible" but "resolvable only by holding the grid
solver years behind current, silently". A study whose power-flow results depend on
which package pip happened to backtrack to is not reproducible, and nobody would
notice — which is worse than an error. The split removes the question.

Note that ADR-0001 alone is sufficient reason for the process boundary. This ADR
means the boundary is also load-bearing for a second, independent reason, so it
should not be removed if the licence question is ever resolved without also
resolving this one.

**Guard.** `kasflex doctor` warns when `gl_gym` is importable from the main
environment.

---

## ADR-0003 — Planners emit intent; only the low-level controller touches actuators

**Decision.** Every planner — rule-based, MPC, language model — returns the same
`Plan` of 24 `IntervalIntent` records. A deterministic low-level controller converts
accepted intent into GreenLight actuator setpoints at 15-minute resolution.

**What forced it.** Two things, and the second is the one that decides the
experiment. First, a language model cannot act reliably at a 15-minute control
resolution. Second, and more important: if the baseline planned actuators while the
language model planned intent, the experiment would be comparing two different
control architectures and would measure the low-level controller rather than the
planner. The requirements are explicit that a fair comparison needs the baseline and
the MPC reference at intent level too (R7, R69 in the source document's numbering).

**Cost.** The planner cannot express anything the intent vocabulary lacks. That is a
real ceiling on achievable performance, and it should be reported as one rather than
attributed to the planner. If a study finds the AI cannot beat the baseline, one
candidate explanation is always that the intent vocabulary was too coarse.

---

## ADR-0004 — Two scales and two time periods, never mixed

**Decision.** Validation runs at the AGC research-compartment scale (96 m², and the
GreenLight default configuration reports 144 m²) over 2019–2020. Scenarios run at
commercial scale (about 5 ha) over 2022 onwards. Every energy figure is reported per
square metre alongside its absolute value, and every result states which period it
used.

**What forced it.** The scales differ by a factor of roughly 350–500. Conflating
them makes every cost figure meaningless. The periods cannot be merged either:
the AGC measurements exist for 2019–2020, while price volatility, grid congestion
and archived weather forecasts only coexist from 2022. Open-Meteo's historical
forecast archive does not reach back to the AGC period at all — verify its actual
coverage before fixing scenario dates.

**How it is enforced.** `EnergyHub.floor_area_m2` is explicit and required;
`DispatchResult.summary()` always reports `net_cost_eur_per_m2` next to
`net_cost_eur`; the worker returns `model_floor_area_m2` and `scale_factor` in its
diagnostics so the scaling applied is visible in every result record.

---

## ADR-0005 — Forecasts plan, actuals score, and the separation is structural

**Decision.** `PlanningContext` contains forecast series only. The runner holds the
actuals and uses them solely for execution and scoring.

**What forced it.** R4, and the requirements' blunt warning that confusing forecast
with actual invalidates every result. A convention would not survive contact with a
codebase; a type that simply does not carry the actuals will.

**Cost.** Some duplication: the greenhouse model is run once on the forecast to give
the planner a heat-demand profile to reason about, and again on the actuals to
execute. Cheap, and the alternative is an oracle.

---

## ADR-0006 — Dispatch is faithful; only the checker decides feasibility

**Decision.** `dispatch_plan` carries out infeasible requests as written and records
the out-of-bounds result. It clips only genuinely physical limits — a full heat
buffer cannot accept more heat — and reports what was dumped.

**What forced it.** The headline measurement is violations with the checker disabled
versus enabled. If dispatch clipped an over-discharge to the battery floor, the
disabled column would read zero and the experiment would measure nothing. A test
asserts this directly (`test_dispatch_does_not_clip_infeasible_requests`).

---

## ADR-0007 — Hard and projected violations are reported separately

**Decision.** Violations decided exactly from the plan and the asset limits are
`HARD`. Violations that depend on a forward model of the greenhouse are `PROJECTED`.
By default only `HARD` violations reject a plan.

**What forced it.** A contract breach is arithmetic. A predicted indoor temperature
inherits the greenhouse model's error, which is not yet quantified (stage 1). Letting
a modelling artefact reject a plan would attribute the model's error to the planner
and quietly turn it into a safety result. `fail_on_projected` makes the stricter
policy available once the model error is known.

---

## ADR-0008 — The realised audit ignores the experimental condition

**Decision.** After execution, every run is audited by a checker with all checks
enabled, whatever the arm under test had switched off.

**What forced it.** R21 requires at least one designated outcome to be left
unverified so that unchecked degradation can be measured. If the audit honoured the
same exclusion, the unverified run would score clean and the measurement would
vanish. A test covers it (`test_audit_uses_all_checks_even_when_one_is_excluded`).

---

## ADR-0009 — The MVP ships an unvalidated surrogate greenhouse, clearly labelled

**Decision.** `SurrogateGreenhouse` is a first-order model that lets the whole
pipeline run on a bare clone with no downloads and no heavy dependencies. Every
`DayOutcome` carries `validated=False`, and the CLI prints a warning on every run
that uses it.

**What forced it.** Acceptance criterion 2 (offline on a laptop) and criterion 7
(one command reproduces every figure) both need the pipeline to run before
GreenLight-Gym2 is set up. Making the real model a hard dependency would put an
AGPL package and a second virtual environment between a new user and their first
result.

**Cost, and the risk.** The surrogate produces plausible-looking numbers that mean
nothing physically. The mitigation is that it is labelled everywhere it appears, and
that stage 1 replaces it. **No figure computed with the surrogate may be published**,
and `provenance.greenhouse_validated` is in every result record so that this can be
checked mechanically rather than remembered.

---

## ADR-0010 — A constraint-blind fixture planner ships alongside the real ones

**Decision.** `NaivePlanner` chases cheap hours and ignores every limit. It is used
in tests and in the offline demo.

**What forced it.** The rule-based baseline never proposes anything unsafe, so with
only the baseline both columns of the headline table read zero and the pipeline
looks correct while proving nothing. Demonstrating that the measurement works
requires a planner that can fail.

**Risk.** Someone reports its numbers as an AI result. It is named `naive`, its
docstring says it is not a model of language-model behaviour, and its plans carry
`notes="Constraint-blind fixture planner. Not an AI result."`.
