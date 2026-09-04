# Contributing

## Before you change anything

Read [docs/DECISIONS.md](docs/DECISIONS.md). Several things that look like
unnecessary complexity are load-bearing, and two of them would keep working if you
removed them — which is what makes them dangerous.

The two that matter most:

1. **Never import `gl_gym` from `src/kasflex/`.** It is AGPL-3.0-or-later. Importing
   it into the Apache-2.0 core would relicense the whole project, and nothing would
   visibly break. Use the subprocess worker. `kasflex doctor` warns if `gl_gym` is
   importable from the main environment.

2. **Never make `dispatch_plan` clip an infeasible request.** It looks like a bug
   that the battery can be driven below its floor. It is not: the checker has to be
   able to see the violation, or the project's headline measurement reads zero for
   the wrong reason. `test_dispatch_does_not_clip_infeasible_requests` guards this.

## Adding a safety check

```python
@check("battery.daily_cycles")
def _cycle_limit(ctx: CheckContext) -> list[Violation]:
    ...
```

Then add a test in `tests/test_checker.py` that constructs a plan violating exactly
that limit and asserts the checker names it. The suite fails until you do — R20
requires every check to be covered by a deliberately invalid plan, and
`test_every_registered_check_has_a_test` enforces it.

A rejection must carry the constraint, the interval, the actual value and the
feasible bound. A rejection the planner cannot act on is barely better than no
rejection at all.

## Hard versus projected

Mark a violation `HARD` only if it is decidable exactly from the plan and the asset
limits. Anything that depends on a forward model of the greenhouse is `PROJECTED`
and does not reject a plan by default. Blurring this line turns a modelling artefact
into a safety result.

## Style

Run `make lint` and `make test`. Line length 100. Type hints on public functions.
Docstrings explain *why*, not *what* — the code already says what.

Units go in the name: `_kw`, `_kwh`, `_eur_kwh`, `_m2`, `_kg`. A unit error in an
energy model is invisible until it has silently changed a published figure. There
was one in the GreenLight worker during development — the heat demand was scaled by
the *area ratio* rather than the floor area, and it produced numbers that looked
plausible (45 kW) instead of correct (6500 kW).

## Reproducibility

Every change must keep these true:

- Same seed and configuration produce the same result record, byte for byte.
- No test touches the network.
- Nothing reads a live API at run time.
