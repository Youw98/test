"""Machine-readable checker output (R17).

A rejection must tell the planner enough to fix the plan without guessing: which
constraint, which interval, what it asked for, and what it may have instead. That
is exactly the four fields of :class:`Violation`, and it is what makes the revision
loop (R18) something better than resampling.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How much confidence the checker has that a violation is real.

    ``HARD`` violations are decided exactly from the plan and the asset limits --
    a contract breach or an out-of-bounds state of charge is arithmetic, not
    prediction. ``PROJECTED`` violations depend on a forward model of the
    greenhouse, so they inherit that model's error. Reporting the two separately
    keeps an honest line between what verification guarantees and what it merely
    expects, and stops a modelling artefact from being counted as a safety result.
    """

    HARD = "hard"
    PROJECTED = "projected"


@dataclass(frozen=True)
class Violation:
    """One violated constraint in one interval."""

    constraint: str
    """Stable identifier, e.g. ``"grid.import_limit"``. Safe to match on in analysis."""
    category: str
    """``"electrical"``, ``"asset"`` or ``"crop"`` -- the R14/R15/R16 grouping."""
    severity: Severity
    hour: int | None
    """Hour of the day, or ``None`` for a whole-day constraint such as the DLI."""
    actual: float
    bound: float
    unit: str
    message: str
    """Human-readable statement, shown to the approver and to the planner."""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["severity"] = self.severity.value
        return payload


@dataclass(frozen=True)
class Verdict:
    """The checker's decision on one plan."""

    accepted: bool
    violations: tuple[Violation, ...] = ()
    checks_run: tuple[str, ...] = ()
    checks_excluded: tuple[str, ...] = ()
    enabled: bool = True
    """False when the checker was switched off entirely (R19). Then ``accepted`` is
    True by construction and ``violations`` is empty -- the run is unverified."""
    plan_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def hard_violations(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity is Severity.HARD)

    @property
    def projected_violations(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity is Severity.PROJECTED)

    def feedback(self, explain: bool = True, limit: int = 12) -> str:
        """Text handed back to the planner for its next revision.

        Args:
            explain: When False, return only that the plan was rejected, with no
                reasons. This is the R19 switch that separates the value of
                *verification* from the value of *explanation*: a planner that
                improves only when told why is a different finding from one that
                improves merely by being rejected.
            limit: Maximum number of violations to list.
        """
        if self.accepted:
            return "Plan accepted."
        if not explain:
            return "Plan rejected. No further information is available."
        lines = [f"Plan rejected: {len(self.violations)} constraint violation(s)."]
        for v in self.violations[:limit]:
            where = "whole day" if v.hour is None else f"hour {v.hour:02d}"
            lines.append(
                f"- [{v.category}/{v.constraint}] {where}: "
                f"{v.actual:.2f} {v.unit} against a limit of {v.bound:.2f} {v.unit}. {v.message}"
            )
        if len(self.violations) > limit:
            lines.append(f"- ... and {len(self.violations) - limit} more.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "enabled": self.enabled,
            "plan_revision": self.plan_revision,
            "checks_run": list(self.checks_run),
            "checks_excluded": list(self.checks_excluded),
            "violations": [v.to_dict() for v in self.violations],
            "metadata": self.metadata,
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)
