"""Client for the GreenLight-Gym2 subprocess worker.

KasFlex core does not import ``gl_gym`` anywhere -- it launches
``workers/greenlight/worker.py`` under a *different* Python interpreter and speaks
JSON to it. That keeps the AGPL obligation and the ``numpy<2`` pin on the far side
of a process boundary (docs/DECISIONS.md ADR-0001, ADR-0002).

The interpreter is found from ``KASFLEX_GREENLIGHT_PYTHON``, falling back to
``.venv-greenlight/bin/python`` in the repository root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kasflex.adapters.greenhouse import DayOutcome
from kasflex.energy.dispatch import HourlyConditions
from kasflex.intent import Plan

DEFAULT_WORKER = Path("workers/greenlight/worker.py")
DEFAULT_VENV_PYTHON = Path(".venv-greenlight/bin/python")


class GreenLightUnavailableError(RuntimeError):
    """Raised when the worker environment is missing or will not start.

    Carries the setup command, because the usual cause is simply that the separate
    virtual environment has not been created yet.
    """


@dataclass
class GreenLightWorker:
    """Drives GreenLight-Gym2 in a separate process.

    Attributes:
        python: Interpreter for the worker environment.
        worker_script: Path to ``worker.py``.
        scenario: GreenLight weather scenario, ``{"location", "growth_year", "start_day"}``.
        seed: Passed to ``env.reset`` for reproducibility (R6).
        timeout_s: Per-request timeout.
        env_kwargs: Extra keyword arguments forwarded to ``gymnasium.make``.
    """

    name: str = "greenlight-gym2"
    python: str = field(default_factory=lambda: os.environ.get(
        "KASFLEX_GREENLIGHT_PYTHON", str(DEFAULT_VENV_PYTHON)
    ))
    worker_script: str = str(DEFAULT_WORKER)
    scenario: dict[str, object] = field(default_factory=dict)
    seed: int = 0
    timeout_s: float = 900.0
    env_kwargs: dict[str, object] = field(default_factory=dict)

    def available(self) -> bool:
        """True when both the interpreter and the worker script exist."""
        return Path(self.python).exists() and Path(self.worker_script).exists()

    def _require(self) -> None:
        if not Path(self.worker_script).exists():
            raise GreenLightUnavailableError(
                f"worker script not found at {self.worker_script!r}. "
                f"Run KasFlex from the repository root, or set worker_script explicitly."
            )
        if not Path(self.python).exists():
            raise GreenLightUnavailableError(
                f"GreenLight worker interpreter not found at {self.python!r}.\n"
                f"Create the separate worker environment:\n"
                f"    python3 -m venv .venv-greenlight\n"
                f"    ./.venv-greenlight/bin/pip install -r workers/greenlight/requirements.txt\n"
                f"or point KASFLEX_GREENLIGHT_PYTHON at an interpreter that has gl-gym "
                f"installed. It must be a different environment from this one: gl-gym "
                f"pins numpy<2 and power-grid-model requires numpy>=2."
            )

    def simulate_day(
        self, plan: Plan, conditions: tuple[HourlyConditions, ...], floor_area_m2: float
    ) -> DayOutcome:
        """Simulate one day through the worker.

        Note that ``conditions`` is accepted for interface compatibility but the
        weather actually comes from GreenLight's own weather files, selected by
        ``scenario``. Prices in ``conditions`` are used by the energy hub, not by
        the greenhouse model. Keeping the two sources explicit is what stops a
        forecast leaking into the physics -- see ADR-0005.
        """
        self._require()
        request = {
            "cmd": "simulate_day",
            "plan": plan.to_dict(),
            "floor_area_m2": floor_area_m2,
            "seed": self.seed,
            "scenario": self.scenario,
            "env_kwargs": self.env_kwargs,
        }
        try:
            proc = subprocess.run(
                [self.python, self.worker_script],
                input=json.dumps(request) + "\n",
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GreenLightUnavailableError(
                f"GreenLight worker timed out after {self.timeout_s:.0f}s"
            ) from exc

        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            raise GreenLightUnavailableError(
                f"GreenLight worker produced no output (exit {proc.returncode}).\n"
                f"stderr:\n{proc.stderr[-2000:]}"
            )
        response = json.loads(lines[-1])
        if not response.get("ok"):
            raise GreenLightUnavailableError(
                f"GreenLight worker failed: {response.get('error')}\n"
                f"{response.get('traceback', '')[-2000:]}"
            )

        o = response["outcome"]
        return DayOutcome(
            heat_demand_kw=tuple(o["heat_demand_kw"]),
            co2_demand_kg_h=tuple(o["co2_demand_kg_h"]),
            temp_c=tuple(o["temp_c"]),
            rh_pct=tuple(o["rh_pct"]),
            co2_ppm=tuple(o["co2_ppm"]),
            fruit_growth_kg_m2=float(o["fruit_growth_kg_m2"]),
            natural_dli_mol_m2=float(o["natural_dli_mol_m2"]),
            model=o.get("model", self.name),
            validated=bool(o.get("validated", False)),
            diagnostics=dict(o.get("diagnostics", {})),
        )


def default_worker_python() -> str:
    """The interpreter KasFlex will use for the worker, for diagnostics output."""
    return os.environ.get("KASFLEX_GREENLIGHT_PYTHON", str(DEFAULT_VENV_PYTHON)) or sys.executable
