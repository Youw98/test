"""Model-predictive reference controller (R8).

**Not implemented in the MVP.** This module fixes the interface and states what
building it involves, so that stage 5 of the plan is a matter of filling in a body
rather than redesigning anything around it.

Why it is deliberately last: the MPC is a *reference*, not a deliverable. Its role
is to bound how much of the gap between the baseline and the AI planner is real
headroom rather than planner skill. That is worth a great deal once the AI planner
exists and nothing at all before it, so it is scheduled after the thing it
measures. The plan cuts from the bottom if time runs short, and this is above only
the optional grid work.

The intended implementation, for whoever picks it up:

* Formulate the hourly energy hub as a mixed-integer linear program over the
  24-hour horizon: continuous battery, buffer and boiler variables, binary CHP
  commitment with min run and min down constraints.
* Take the forecast conditions from :class:`~kasflex.controllers.base.PlanningContext`
  -- never the actuals, or the reference becomes an oracle and the comparison is
  meaningless.
* Solve with CasADi, which is already a GreenLight-Gym2 dependency and so adds
  nothing new to the worker environment.
* Round the solution onto the intent vocabulary and return a
  :class:`~kasflex.intent.Plan`, so the MPC is compared on exactly the same terms
  as every other planner.
"""

from __future__ import annotations

from dataclasses import dataclass

from kasflex.controllers.base import PlanningContext
from kasflex.intent import Plan


class MpcNotImplementedError(NotImplementedError):
    """Raised by :class:`MpcPlanner` until stage 5 of the MVP plan lands."""


@dataclass
class MpcPlanner:
    """Placeholder for the MPC reference. Raises rather than pretending."""

    name: str = "mpc"
    horizon_hours: int = 24

    def plan(self, context: PlanningContext) -> Plan:
        raise MpcNotImplementedError(
            "The MPC reference controller is stage 5 of the MVP plan and is not "
            "implemented yet. Use --planner rule-based or --planner llm. "
            "See src/kasflex/controllers/mpc.py for the intended formulation."
        )
