# KasFlex MVP plan

**Status:** stages 0 and 2 built and tested; stage 3 partly built. Everything below
is either working code in this repository or a stated next step, and the difference
is marked throughout.

---

## 1. The one measurement

Strip away the architecture and KasFlex exists to fill in four cells:

| | Checker disabled | Checker enabled |
|---|---|---|
| Operating cost | — | — |
| Limit violations | — | — |

Every design decision in this repository is answerable to that table. The most
common way to fail here is not to build the wrong thing — it is to build something
that *looks* right and quietly produces zeros in both columns. Three of the
decisions in [DECISIONS.md](DECISIONS.md) (ADR-0006, ADR-0008, ADR-0010) exist only
to stop that happening.

**As of today, on synthetic data with a fixture planner, the table fills in:**

```
condition               runs    cost EUR  violations   hard  fallback   peak kW
-------------------------------------------------------------------------------
rule-based                 3    10498.15        0.00      0      0.00      5718
ai-unverified              3    11601.63       66.00    198      0.00     10650
ai-verified                3    10498.15        0.00      0      1.00      5718
ai-verified-silent         3    10498.15        0.00      0      1.00      5718
ai-verified-gap            3    10498.15        0.00      0      1.00      5718
```

Reproduce with `kasflex experiment --days 3`. These numbers are **not results**:
the greenhouse model is the unvalidated surrogate and the planner is a fixture. They
demonstrate that the measurement apparatus works, which is the only thing the MVP
owes at this stage.

---

## 2. What already exists, and must not be rebuilt

The strongest thing about this project's position is how little of it is new. The
following were checked, not assumed, on 2026-09-04.

| Component | Source | Licence | Verified finding |
|---|---|---|---|
| Greenhouse and crop physics | [GreenLight-Gym2](https://github.com/BartvLaatum/GreenLight-Gym2) | **AGPL-3.0-or-later** | `pip install gl-gym` works; a 24-hour simulation runs in **0.09 s**; default branch is `master`, not `main` |
| Rule-based control logic | Same, `RuleBasedController` | AGPL | Ships with documented parameters in `configs/agents/rule_based.yml` |
| Underlying model | [GreenLight](https://github.com/davkat1/GreenLight) | — | Reimplemented in CasADi by GL-Gym; ~17x faster |
| Grid power flow | [power-grid-model](https://github.com/PowerGridModel/power-grid-model) | MPL-2.0 | Batch power flow over 24 hourly profiles verified working |
| Grid data science layer | [power-grid-model-ds](https://github.com/PowerGridModel/power-grid-model-ds) | MPL-2.0 | Available; not needed for the MVP's single feeder |
| Agent patterns | [Power-Agent](https://github.com/Power-Agent) — PowerMCP, PowerSkills, PowerAgentBench | MIT | Reference only, see §7 |

**Three findings changed the design.** Each is written up as an ADR.

1. **GL-Gym is AGPL-3.0-or-later.** It cannot be imported by an Apache-2.0 core
   that will also be served over a network. → ADR-0001, process boundary.
2. **`gl-gym` pins `numpy<2`; `power-grid-model` needs `numpy>=2`.** They can only
   be co-installed by pip silently downgrading the grid solver to a 2023-era
   release. → ADR-0002, separate environments.
3. **GL-Gym already has the extension points we need.** `BasePriceModel`,
   `BaseReward`, `BaseObservations`, a configurable `weather_data_dir` and a
   documented 10-column disturbance format. **No fork is required** to put ENTSO-E
   prices and Dutch weather into the model — a subclass suffices. This was the
   single biggest risk to the schedule and it is retired.

On the last point, one wrinkle to be aware of when stage 3 lands: `GreenhouseReward`
constructs `FixedPrice(elec_price)` in its `__init__` rather than accepting a price
model as a keyword. Real hourly prices therefore need a `GreenhouseReward` subclass
that reassigns `self.elec_price_model` after `super().__init__()`. GL-Gym's stock
`HourlyPriceTrajectory` is keyed on hour-of-day, so it expresses a repeating diurnal
shape, not a multi-day series — a KasFlex price model keyed on `(day_of_year,
hour_of_day)` is needed. Both are small; neither needs a fork.

---

## 3. What KasFlex adds

Everything KasFlex contributes sits in the space between those components:
market prices, the energy hub, the intent abstraction, the safety checker, human
approval, and the experiment harness. See [ARCHITECTURE.md](ARCHITECTURE.md).

The one structural departure from the Alliander proposal's Figure 1 is that
**safety is not a sidecar**. There, the safety and validation layer sits beside the
agents. Here it sits *between* the planner and everything downstream: no intent
reaches an actuator without a verdict from pure, deterministic code that has no
dependency on the planner. That is what makes the headline table a measurement
rather than an assertion.

---

## 4. Build order

Stages are ordered so that each one is useful on its own and the last ones can be
cut. **If time runs short, cut from the bottom.** The checker (R13–R21) and human
approval (R22–R25) are never cut: they carry the research contribution.

### Stage 0 — Skeleton that runs end to end ✅ built

Intent schema, energy hub, dispatch, checker, rule-based baseline, runner,
experiment matrix, CLI, FAIR metadata, 107 tests at 94% coverage.

*Complete when:* `kasflex experiment` runs offline on a bare clone. **It does.**

### Stage 1 — GreenLight-Gym2, validated against AGC ⬜ next

The worker exists and drives the real model today; what is missing is the
validation.

- Download AGC 2nd edition (D1). Read Hemming et al., *Sensors* 2020, **first**.
- Configure GL-Gym to the AGC compartment: floor area, lamp power, heating capacity,
  screens, and the actual 2019–2020 weather.
- Run the AGC reference compartment's realised setpoints through the model and
  compare simulated against measured heating, electricity and CO2 consumption.
- **Publish the deviation, whatever it is.** Acceptance criterion 1 asks for the
  deviation to be quantified, not for it to be small.

*Complete when:* model error is quantified and written into `docs/VALIDATION.md`.
*Risk:* the AGC dataset is large and heterogeneous; budget time for reconciling its
actuator logs with GL-Gym's six control channels. Its energy data is the reason D1
is the right dataset and D4/D5 are not.

### Stage 2 — Safety checker ✅ built

Thirteen named, individually switchable checks across electrical, asset and crop
limits. Machine-readable rejections carrying constraint, interval, actual value and
feasible bound. A test asserts every registered check has a deliberately invalid
plan exercising it.

*Complete when:* every constructed invalid plan is detected. **It is.**

*Remaining:* the crop climate bands are `PROJECTED` and currently run against the
surrogate's projection. They become meaningful after stage 1.

### Stage 3 — Real data and real prices 🔶 partly built

Built: dataset registry with DOIs and licences, checksummed cache with a provenance
manifest, deterministic synthetic fallback.

To do:

- ENTSO-E day-ahead fetcher via `entsoe-py`, cached to parquet. Needs an API key.
- KNMI measured weather (**actuals**) and Open-Meteo archived forecasts
  (**forecasts**) — these are two requirements, not alternatives (ADR-0005).
  Verify Open-Meteo's actual coverage before fixing scenario dates.
- TTF gas, daily resolution.
- A GL-Gym `BasePriceModel` subclass keyed on `(day_of_year, hour_of_day)`, plus the
  `GreenhouseReward` subclass described in §2.
- Convert KNMI data to GL-Gym's 10-column weather format under `weather_data_dir`.

*Complete when:* a full day runs on real 2023 data with no AI and a plausible cost.

### Stage 4 — Language-model planner ⬜

The planner, prompt, trace store and replay path are built and tested against a
fake transport. What remains is real:

- Wire `anthropic_call_fn`; run against three or more models (R11).
- Record every call; verify a recorded run replays with no API access (R12).
- Tune the prompt against checker feedback, not against outcomes — the point is
  whether structured rejection makes a planner correct itself.

*Complete when:* a plan passes the checker and is comparable to the baseline.
*Risk:* the model produces valid JSON that is strategically poor. That is a finding,
not a bug — acceptance criterion 5 asks for exactly one such case to be reported.

### Stage 5 — MPC reference ⬜

Interface fixed in `controllers/mpc.py`, which raises rather than pretending. Build
it as a 24-hour MILP over the hub — continuous battery, buffer and boiler; binary
CHP commitment with min run and down constraints — solved with CasADi, which
GL-Gym already depends on. It must plan at intent level (ADR-0003) and see only
forecasts.

*Complete when:* the gap between baseline and reference is measurable.
*Why last:* it bounds how much of the baseline-to-AI gap is real headroom rather
than planner skill. That is valuable once the AI planner exists and worthless
before it.

### Stage 6 — Browser interface ⬜

Plan with per-interval reasoning; approve, edit and reject; re-verification on edit;
all controllers compared on one scenario with violation counts; cost and crop
outcome first, energy units second; a permanent "simulation, not validated for
operational use" notice; runs offline from cached data.

*Complete when:* an unfamiliar user runs a scenario unaided in 10 minutes.
*Note:* serving this over a network is precisely what makes the AGPL question in
ADR-0001 load-bearing. Keep the interface in the Apache-2.0 core.

### Stage 7 — Fleet and grid 🔶 optional, adapter built

`adapters/grid.py` builds a radial MV feeder in `power-grid-model` and runs a batch
power flow over the 24 hourly net positions, returning voltage and loading
violations. A test shows the premise directly: staggering three greenhouses produces
strictly fewer violations than switching them simultaneously.

*Complete when:* a cluster scenario shows congestion relief from staggering.
*This is the bridge to the Alliander proposal.* It is optional for the 4TU
demonstrator and central to any KIC follow-on, which is why the adapter is built
now even though the stage is not.

---

## 5. Requirement coverage

| # | Requirement | Status | Where |
|---|---|---|---|
| R1 | GreenLight-Gym2 for climate and crop | 🔶 wired, unvalidated | `workers/greenlight/worker.py` |
| R2 | Battery, CHP, boiler, buffer, PV, grid | ✅ | `energy/assets.py` |
| R3 | Historical ENTSO-E and TTF prices | ⬜ stage 3 | `data/registry.py` |
| R4 | Forecast and actual strictly separate | ✅ | `controllers/base.py`, ADR-0005 |
| R5 | Per-day cost, gas, revenue, DLI, band hours | ✅ | `dispatch.summary()`, `run.py` |
| R6 | Identical seed gives identical results | ✅ | `tests/test_reproducibility.py` |
| R7 | Rule-based baseline at intent level | ✅ | `controllers/rule_based.py` |
| R8 | MPC reference via CasADi | ⬜ stage 5 | `controllers/mpc.py` |
| R9 | AI planner, documented schema | ✅ | `controllers/llm.py`, `intent.py` |
| R10 | Deterministic low-level controller | ✅ | worker + `dispatch.py` |
| R11 | Three or more models by configuration | ✅ | `LlmPlanner.model` |
| R12 | Record traces, replay without API | ✅ | `TraceStore` |
| R13 | Verify every plan deterministically | ✅ | `checker/rules.py` |
| R14 | Electrical limits | ✅ | 4 checks |
| R15 | Asset limits | ✅ | 5 checks |
| R16 | Crop limits | ✅ | 4 checks |
| R17 | Machine-readable rejection reason | ✅ | `checker/verdict.py` |
| R18 | N revisions then baseline | ✅ | `run.py` |
| R19 | Checker and explanation switchable independently | ✅ | `CheckerConfig` |
| R20 | Unit tests with invalid plans | ✅ | `tests/test_checker.py` |
| R21 | Exclude a designated outcome | ✅ | `excluded_checks`, ADR-0008 |
| R22 | Plan with reasoning; approve/edit/reject | 🔶 API built, no UI | `oversight.py` |
| R23 | Re-verify user edits | ✅ | `run.py` |
| R24 | Plain-language brief | ✅ | `OperatorBrief` |
| R25 | Append-only log | ✅ | `AuditLog` |
| R26 | Interaction metrics, anonymous mode | 🔶 fields exist | `oversight.py` |
| R27 | Browser-based, 10 minutes unaided | ⬜ stage 6 | — |
| R28 | Cost and crop first | ⬜ stage 6 | — |
| R29 | All controllers compared | ✅ CLI, ⬜ UI | `experiment.py` |
| R30 | Fully offline from cache | ✅ | `tests/test_end_to_end.py` |
| R31 | Permanent simulation notice | 🔶 CLI yes, UI pending | `cli.py` |
| R32 | Matrix unattended from CLI | ✅ | `kasflex experiment` |
| R33 | One structured record per run | ✅ | `RunResult.to_record()` |
| R34 | OSI licence, docs, CITATION.cff, provenance | ✅ | repository root |
| R35 | Regenerate every figure from a script | 🔶 records yes, figures pending | `make reproduce` |

✅ 24 · 🔶 7 · ⬜ 4

---

## 6. Acceptance criteria

| # | Criterion | How it is demonstrated | Status |
|---|---|---|---|
| 1 | Simulated vs AGC measured consumption, deviation published | Stage 1 | ⬜ |
| 2 | Scenarios run offline on a laptop | `test_runs_offline_with_no_network` blocks the socket module | ✅ |
| 3 | Violations measurable disabled, zero enabled | `test_checker_disabled_produces_violations_enabled_produces_none` | ✅ |
| 4 | Four arms comparable on identical scenarios | `kasflex experiment`; MPC arm records an error, not a silent gap | 🔶 |
| 5 | At least one case where AI is worse than baseline | `test_unverified_planner_can_be_worse_than_the_baseline` | ✅ apparatus |
| 6 | Matrix runs unattended, structured results | `test_experiment_matrix_runs_unattended_and_writes_records` | ✅ |
| 7 | External researcher reproduces figures with one command | `make reproduce` | 🔶 |

Criteria 3 and 5 are marked as apparatus: the mechanism is proven, but the numbers
come from a fixture planner and an unvalidated model, so they are not yet findings.

---

## 7. On Power-Agent

The [Power-Agent](https://github.com/Power-Agent) organisation is worth reading and
mostly worth *not* adopting.

| Repository | Relevance |
|---|---|
| **PowerSkills** | The most useful. Its "escalation triggers" table — map an observation to a mitigation playbook — is a good model for turning checker rejections into planner guidance. Its per-skill layout (`SKILL.md`, `scripts/`, `references/`) is a sound template if KasFlex ever grows a skill surface. |
| **PowerAgentBench** | The benchmark shape is the right reference for KasFlex's experiment matrix: standard scenarios, standard metrics, multi-step operational tasks. |
| **PowerMCP** | MCP servers for PowerWorld, PSS/E, OpenDSS, pandapower, PyPSA. Transmission-oriented and commercial-tool-oriented. |
| **PowerFM**, **PowerWF** | Foundation models and workflows. Out of scope. |

**Recommendation: reference patterns, do not take a dependency.** PowerMCP's servers
wrap transmission planning tools; KasFlex needs LV/MV distribution behind a single
connection, which `power-grid-model` covers directly and which is the Dutch DSO's
own tool. Adding an MCP layer would add a protocol boundary without adding a
capability. The requirements document already reaches this conclusion by listing
Power-Agent under "reference patterns only", and looking at the repositories
confirms it.

The one thing worth stealing outright is the escalation-triggers idea, in stage 4:
when the checker rejects, the feedback could name not just the violated constraint
but the standard remedy for it. The checker's messages already gesture at this
("Reduce lighting, stop charging the battery, or run the CHP in this hour").

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| AGPL contaminates the core, unnoticed | High | Process boundary, `kasflex doctor` warning, ADR-0001 |
| Silent `power-grid-model` downgrade breaks reproducibility | Medium | Separate environments, ADR-0002 |
| Surrogate numbers get published as results | High | `validated=False` in every record, CLI warning, ADR-0009 |
| Fixture planner numbers reported as an AI result | Medium | Named `naive`, plan carries a disclaiming note, ADR-0010 |
| Open-Meteo forecast archive does not cover chosen dates | Medium | Verify coverage before fixing dates; two-period rule, ADR-0004 |
| Scale confusion between 96 m² and 5 ha | High | Per-m² reported always; scale factor in worker diagnostics |
| Checker rejects everything, AI never gets a plan through | Medium | Fallback to baseline (R18); baseline verified clean across 40 scenario variants |
| Intent vocabulary too coarse to beat the baseline | Medium | Report as a ceiling, not as planner failure, ADR-0003 |
| ENTSO-E outage during a demonstration | Low | Nothing reads a live API at run time (R30) |

---

## 9. Out of scope

Real-time control · SCADA or process-computer connection · live market trading ·
national grid modelling · operational advice to growers · training new AI models ·
any connection to physical greenhouse equipment.

KasFlex is a simulation. Nothing in it should be represented as validated for
operational use, and the interface will say so permanently (R31).
