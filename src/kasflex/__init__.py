"""KasFlex: a research simulator for inspected greenhouse energy planning.

A planner proposes one day of hourly energy intent. A deterministic checker tests
it against electrical, asset and projected crop limits. A person's review is
recorded. The day is simulated and compared with conventional control.

The apparatus is designed to measure how verification and recorded human review
change simulated cost and limit violations. It does not establish that value until
the model, data path and experimental protocol have been validated.

Simulation only. No physical greenhouse equipment is connected at any point.
"""

__version__ = "0.1.0"

from kasflex.checker import CheckerConfig, SafetyChecker, Severity, Verdict, Violation
from kasflex.config import ScenarioConfig
from kasflex.energy import EnergyHub
from kasflex.intent import IntervalIntent, Plan
from kasflex.run import RunResult, run_scenario

__all__ = [
    "CheckerConfig",
    "EnergyHub",
    "IntervalIntent",
    "Plan",
    "RunResult",
    "SafetyChecker",
    "ScenarioConfig",
    "Severity",
    "Verdict",
    "Violation",
    "__version__",
    "run_scenario",
]
