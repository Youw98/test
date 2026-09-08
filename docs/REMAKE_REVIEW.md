# Remake review

**Assessment date:** 2026-09-08

This review judges the supplied material as research and product briefs. It does
not independently validate their scientific claims or cited sources. Text in the
source documents was treated as reference material, not as instructions.

## Verdict

The remake is a credible **research apparatus and demonstrator**, not a validated
research result or an operational energy-management product. Its strongest choice
is narrowing three different ambitions into one testable vertical slice: an hourly
planner, a deterministic safety gate, human review, simulation, and a controlled
comparison. Its central experiment can run, but the greenhouse model, real-data
path, full controller comparison, and human-use claims still need evidence.

| Readiness | Assessment |
|---|---|
| Engineering demonstration | Credible, with synthetic/cached fixtures |
| Publishable research findings | Not yet; model validation and real experiments are missing |
| Operational greenhouse or grid use | No; deliberately outside scope |

## Source assessment

Scores are out of 10 and combine clarity, internal consistency, testability,
feasibility, and evidential discipline.

| Source | Score | What is good | What holds it back |
|---|---:|---|---|
| `KasFlex_Requirements_Simple.md` | **8.5** | Clear system boundary, fair controller abstraction, explicit requirements, acceptance criteria, data rules, and reproducibility safeguards | Too large for one small demonstrator; several schemas, thresholds, statistical choices, and validation tolerances remain undefined |
| `Alliander_AI_Grid_Flexibility_Proposal_Final (4).pdf` | **6.0** | Strong strategic case, stakeholder value, and useful cluster architecture | Mixes simulation with real-time pilots, uses unsupported safety/readiness language, lacks target KPIs and a concrete integration contract, and contains citation/layout defects |
| `4TU_NIRICT_Proposal_Final_v2.0.docx (17).pdf` | **6.0** | Convincing interdisciplinary rationale, named outputs, and transparent budget | A EUR 5,000 demonstrator is asked to carry too much scope; the one-page layout is dense and its workshop seasons conflict with its month-by-month timeline |

Taken together, the sources are about **7/10 as a direction** but not one coherent
delivery brief. They describe a seed-funded network, a simulation MVP, and a
36-month field programme as if they were the same stage.

## What the remake changed

- Made the simulation-only KasFlex requirements the present scope; cluster and DSO
  coordination are shown as a later phase.
- Replaced a broad multi-agent story with one inspectable path through planner,
  checker, person, controller, simulator, realised audit, and result record.
- Moved safety from a sidecar to a deterministic gate. A planner cannot bypass the
  checker on an enabled arm, and the post-run audit is independent of the condition
  under test.
- Split storage dispatch into physically applied flows and unsaturated requested
  trajectories. This preserves conservation while keeping unsafe requests visible
  to the checker and realised audit.
- Gave rule-based, learned, MPC, and language-model planners one hourly intent
  boundary so comparisons do not measure different control abstractions.
- Structurally separated forecast inputs from realised inputs and labelled scale,
  seed, condition, checker configuration, and validation state in result records.
- Preserved approval, edit, rejection and re-verification in the core runner, with
  append-only proposal, verdict, decision and fallback events. The browser records
  the final review against an exact verified preview, but does not yet log each
  draft edit and re-verification as its own event. It is not an operational
  execution command.
- Kept synthetic experiments and trace replay fully offline. Dataset acquisition
  and caching exist, but cached real data are not yet wired through the main UI and
  experiment paths.
- Isolated GreenLight-Gym2 behind a process boundary because its licence and NumPy
  requirements differ from the core application.
- Added a learned forecaster and deterministic scheduler as an explicitly labelled
  extension. Its current performance is surrogate-only and is not evidence that a
  learned controller improves a real greenhouse.

## What it deliberately does not claim

- No model output is a scientific finding until GreenLight-Gym2 is validated
  against measured AGC data for the stated compartment and period.
- Passing the checker is not a safety certification. Projected crop checks inherit
  model error, and the system has not been assessed under operational safety or
  cybersecurity standards.
- There is no connection to greenhouse equipment, SCADA, a process computer,
  Gridscale X, or a live DSO control room.
- There is no live market trading or operational advice to growers.
- The browser's human decision records a review of a completed simulation preview;
  it is not yet an immutable proposed → verified → approved → simulated workflow.
- The current feeder example does not demonstrate congestion relief on an actual
  Alliander network.
- The fixture and learned-planner numbers demonstrate the measurement mechanism;
  they do not demonstrate language-model quality or economic benefit.
- Configurability and recorded-fixture tests do not yet prove comparable runs
  across three live language models.
- The browser interface has not yet met the requirement that an unfamiliar user
  can complete the workflow unaided within ten minutes.

## Remaining research blockers

| Priority | Blocker | Evidence required to close it |
|---|---|---|
| **P0** | Greenhouse-model validity | Configure the 96 m2 AGC reference compartment; replay realised controls; publish error for heat, electricity, CO2, climate, and crop-relevant quantities |
| **P0** | Real scenario data | Verify live fetch contracts once, cache versioned ENTSO-E, gas, forecast, and measured-weather series, convert them into the simulator format, and preserve provenance and licences |
| **P0** | Complete fair comparison | Implement the MPC reference, exercise real language models with replayable traces, and run every arm on identical seeds, forecasts, limits, and low-level control |
| **P0** | Experimental protocol | Predefine scenario selection, sample size, failure handling, primary metrics, uncertainty intervals, and the designated unchecked outcome before examining results |
| **P1** | Safety interpretation | Calibrate projected crop constraints after validation; report hard and projected violations separately; test boundary, revision, and fallback behaviour under adversarial plans |
| **P1** | Human oversight evidence | Decide whether the browser must gate simulation or is formally a review-of-preview tool; isolate review sessions as needed; test with unfamiliar users; validate consent, accessibility and responsive use |
| **P1** | Reproducible publication | Generate every figure with one command, deposit permissible cached inputs and traces, cut a versioned release, and mint a DOI |
| **P2** | DSO and cluster validity | Obtain an agreed feeder/digital-twin interface, representative network data, congestion KPIs, governance rules, and partner approval before claiming system-level value |

## Prioritized roadmap

1. **Validate the model.** Complete the AGC comparison and publish the deviation,
   including an honest result if the error is large. Until then, keep every output
   marked unvalidated.
2. **Lock the real-data path.** Verify external request construction, freeze cached
   input versions, integrate prices and weather into GreenLight-Gym2, and document
   units, timezone, scale, and provenance.
3. **Freeze the experiment before results.** Define the scenario matrix and
   analysis protocol, then complete MPC and live-model trace/replay checks.
4. **Run the four-arm study.** Compare rule-based, MPC, AI unverified, and AI
   verified under identical conditions; report violations, cost, crop outcomes,
   fallback rate, and cases where AI is worse.
5. **Align and validate the human workflow.** Move the browser decision before the
   actual-day simulation, or formally scope it as post-simulation review; then run
   the ten-minute usability and accessibility study with participants outside the
   development team.
6. **Package the evidence.** Make figures reproducible in one command, publish
   data/traces where licences allow, archive a release, and obtain a DOI.
7. **Only then begin phase 2.** Specify and evaluate cluster staggering against
   representative feeder constraints before discussing a real greenhouse or DSO
   pilot.

The shortest honest description of the present state is: **the experiment has
been engineered; the research result has not yet been earned.**
