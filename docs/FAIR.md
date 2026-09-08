# FAIR principles

How this repository meets Findable, Accessible, Interoperable and Reusable, and
where it currently falls short. The gaps are listed because a FAIR claim without
them is not worth making.

## Findable

| | |
|---|---|
| **F1** Globally unique persistent identifier | ⬜ **Gap.** Mint a DOI by connecting the repository to Zenodo and cutting a release. Do this at first public release, not later — a citable artefact with no DOI gets cited as a URL that will rot. |
| **F2** Rich metadata | ✅ `CITATION.cff` and `codemeta.json`, both machine-readable and validated in CI. |
| **F3** Metadata includes the identifier | 🔶 Ready; add the DOI to `CITATION.cff` and the README badge on release. |
| **F4** Registered and indexed | 🔶 GitHub indexes now; Zenodo and the 4TU.NIRICT community repository on release. |

## Accessible

| | |
|---|---|
| **A1** Retrievable by identifier over an open protocol | ✅ Public git over HTTPS; `pip install` from source. |
| **A1.2** Authentication where necessary | ✅ Only ENTSO-E needs a key, and only to *fetch*. Cached data and every published result are readable without one. |
| **A2** Metadata persists even if the data does not | ✅ The dataset registry records DOIs, licences and access routes independently of whether the files are present. `data/cache/MANIFEST.json` keeps provenance for every cached series. |

## Interoperable

| | |
|---|---|
| **I1** Formal, shared, broadly applicable representation | ✅ Plans and verdicts are JSON against a documented schema; results are JSON Lines; cached series are parquet or CSV; configuration is YAML. |
| **I2** Vocabularies that follow FAIR | 🔶 The intent vocabulary is documented and stable but project-specific. No standard exists for greenhouse energy intent; aligning with one (or proposing one) is a genuine research output for workshop 2. |
| **I3** Qualified references to other data and software | ✅ `CITATION.cff` references GreenLight-Gym2 with its DOI, `power-grid-model`, and the AGC dataset with its DOI, each with a note on how it is used. |

## Reusable

| | |
|---|---|
| **R1** Richly described with accurate attributes | 🔶 Result records carry scenario and planner labels, checker configuration, seed, data source, validation state, plan, verdict, realised violations, a hub snapshot and input hashes. They do not embed the complete hourly inputs or requested/applied dispatch trajectories. |
| **R1.1** Clear and accessible licence | ✅ Apache-2.0 for the core; the AGPL worker is separated and labelled with an SPDX header. See ADR-0001. |
| **R1.2** Detailed provenance | 🔶 Dataset registry, checksummed cache manifest and the core runner's append-only workflow log are present. Real input deposits are absent, and the browser logs final decisions but not each draft edit and re-verification event. |
| **R1.3** Domain-relevant community standards | 🔶 CITATION.cff and CodeMeta are met. Energy-domain standards (CIM/IEC 61970) are not, and are not obviously worth it at this scale — `power-grid-model`'s own model definition is the de facto standard in the Dutch DSO context. |

## Reproducibility, concretely

Beyond FAIR, three properties make a result checkable:

1. **Determinism (R6).** Same seed and configuration, same record, byte for byte.
   Asserted in `tests/test_reproducibility.py`.
2. **Offline fixtures (partial R30).** Synthetic runs read no live API. A test
   replaces the `socket` module and fails if they try to connect. This does not yet
   prove that the main experiment replays cached external inputs, because that
   cache is not wired into the main run/UI/experiment paths.
3. **Replayable agent mechanism (R12).** The trace store keys records on a hash of
   the model and prompt and can replay without an API. Tests use a fake transport;
   real multi-model traces have not yet been acquired or deposited.

## What would make this properly FAIR

In priority order:

1. Mint a Zenodo DOI and add it to `CITATION.cff` and the README.
2. Publish the cached input series (where licences allow) as a versioned data
   deposit, so the provenance manifest points at something permanent.
3. Deposit the recorded planner traces alongside the results. They are the only way
   an external reviewer can re-derive an agent result without spending money.
4. Add `make reproduce` figure generation, so criterion 7 is literally one command.
