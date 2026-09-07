"""KasFlex: verified agentic energy management for greenhouse horticulture.

An AI planner proposes one day of hourly energy intent. A deterministic safety
checker verifies it against electrical, asset and crop limits. A person approves it.
The day is then simulated and compared against conventional control.

The system exists to produce one measurement: what verification and human oversight
are worth, in operating cost and in limit violations.

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
