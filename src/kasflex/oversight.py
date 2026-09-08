"""Human oversight: approval, edits, and the append-only record (R22-R26).

The approval step is not decoration. In the study design it is the second half of
the research contribution -- the checker says what is *permitted*, the human says
what is *wanted* -- and it is the part that maps onto the EU AI Act's human
oversight expectations for a system acting on critical infrastructure.

Three approver implementations are provided: automatic approval for unattended
experiment runs, rejection for testing the fallback path, and a callback for the
browser interface to plug into.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from kasflex.checker.verdict import Verdict
from kasflex.intent import Plan


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


@dataclass(frozen=True)
class HumanDecision:
    """What the operator did with a plan."""

    decision: Decision
    plan: Plan
    """The plan as decided on. For :attr:`Decision.EDIT` this is the edited plan,
    which must be re-verified before acceptance (R23)."""
    comment: str = ""
    seconds_to_decide: float | None = None
    """Interaction metric, recorded only under informed consent (R26)."""


@runtime_checkable
class Approver(Protocol):
    def review(self, plan: Plan, verdict: Verdict) -> HumanDecision: ...


@dataclass
class AutoApprover:
    """Approves whatever the checker accepted. Used for unattended runs (R32).

    This is not a stand-in for a human in results that make claims about human
    oversight -- a run using it is stamped ``operator="auto"`` in the audit log so
    that such a claim cannot be made by accident.
    """

    name: str = "auto"

    def review(self, plan: Plan, verdict: Verdict) -> HumanDecision:
        return HumanDecision(
            decision=Decision.APPROVE if verdict.accepted else Decision.REJECT,
            plan=plan,
            comment="automatic decision, no human involved",
        )


@dataclass
class CallbackApprover:
    """Delegates to a callable, which is how the browser interface plugs in."""

    callback: Callable[[Plan, Verdict], HumanDecision]
    name: str = "callback"

    def review(self, plan: Plan, verdict: Verdict) -> HumanDecision:
        return self.callback(plan, verdict)


class AuditLog:
    """Append-only JSONL record of agent actions, verdicts and human decisions (R25).

    Append-only is the point: entries are never rewritten, so the record of what was
    proposed, what was rejected and why, and what a person decided, survives even
    when a run is later re-analysed. Each entry carries a UTC timestamp.
    """

    def __init__(self, path: str | Path, anonymous: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.anonymous = anonymous
        """When True, operator identity is recorded as ``"anonymous"`` (R26)."""

    def append(self, kind: str, payload: dict[str, Any], operator: str = "") -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "operator": "anonymous" if self.anonymous else operator,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


@dataclass
class OperatorBrief:
    """The plain-language instruction a grower gives before planning (R24)."""

    text: str = ""
    price_ceiling_eur_kwh: float | None = None
    light_target_mol_m2: float | None = None
    events: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [self.text] if self.text else []
        if self.price_ceiling_eur_kwh is not None:
            parts.append(f"Do not buy power above {self.price_ceiling_eur_kwh:.3f} EUR/kWh.")
        if self.light_target_mol_m2 is not None:
            parts.append(f"Aim for {self.light_target_mol_m2:.1f} mol/m2 of supplemental light.")
        parts.extend(self.events)
        return " ".join(parts)
