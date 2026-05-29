"""Pydantic models for duet WorkProducts, Critiques, Scores, plus convergence check."""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, ConfigDict


Severity = Literal["blocker", "major", "moderate", "minor", "nit"]

# Severities that gate acceptance: even if scores are high, an open
# blocker/major/moderate prevents the artifact from being accepted.
# minor and nit items become "suggested improvements" surfaced to the user.
BLOCKING_SEVERITIES = ("blocker", "major", "moderate")
SUGGESTION_SEVERITIES = ("minor", "nit")
Role = Literal["counter_drafter", "critic", "verifier", "roster_proposer"]


class CritiqueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., description="Stable id for this finding within the critique.")
    severity: Severity
    finding: str = Field(..., description="What is wrong or risky.")
    suggested_fix: str = Field(..., description="Concrete change the author should make.")
    addressed: bool = Field(default=False, description="Marked true when the next draft resolves this item.")


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = Field(..., ge=0, le=100, description="0-100 score of the candidate.")
    rationale: str = Field(..., description="One-paragraph justification keyed to the rubric.")
    rubric_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description="Optional per-dimension scores (e.g. {'accuracy': 95, 'clarity': 92}).",
    )


class WorkProduct(BaseModel):
    """One model's full output for a single turn."""
    model_config = ConfigDict(extra="forbid")
    role: Role
    candidate_id: str = Field(..., description="Identifier of the artifact being scored / drafted.")
    counter_draft: Optional[str] = Field(
        default=None,
        description="The counter-draft text (only set when role=counter_drafter or as part of a critique with revision).",
    )
    score_of_candidate: Optional[Score] = Field(
        default=None, description="Score this model assigns to candidate_id."
    )
    critique_items: List[CritiqueItem] = Field(
        default_factory=list, description="Open findings about candidate_id."
    )
    notes: str = Field(default="", description="Free-form rationale or commentary.")


class IterationRecord(BaseModel):
    """One full exchange in the inner loop, persisted to job state."""
    model_config = ConfigDict(extra="forbid")
    n: int
    candidate_id: str
    candidate_text: str
    opus_self_score: Score
    gpt_score_of_opus: Score
    gpt_critique: List[CritiqueItem]
    opus_score_of_gpt: Optional[Score] = None
    opus_critique_of_gpt: List[CritiqueItem] = Field(default_factory=list)
    gpt_counter_draft: Optional[str] = None


def convergence_check(
    score_a: Score,
    score_b: Score,
    open_critique_items: List[CritiqueItem],
    threshold: int = 95,
) -> tuple[bool, str]:
    """Legacy strict-convergence: both scores >= threshold AND zero open items.
    Retained for tests + backwards compatibility. New acceptance gate is
    `acceptance_check` below.
    """
    open_unaddressed = [c for c in open_critique_items if not c.addressed]
    if score_a.value < threshold:
        return False, f"score_a={score_a.value} below threshold={threshold}"
    if score_b.value < threshold:
        return False, f"score_b={score_b.value} below threshold={threshold}"
    if open_unaddressed:
        ids = ",".join(c.id for c in open_unaddressed)
        return False, f"{len(open_unaddressed)} unaddressed critique items: {ids}"
    return True, "converged"


# New acceptance gate (preferred over `convergence_check`). Mirrors the
# canonical implementation in
# C:\Users\acor8\.claude\skills\duet\scripts\convergence.py; keep them in sync.

BLOCKING_SEVERITIES_RUNTIME = ("blocker", "major", "moderate")
SUGGESTION_SEVERITIES_RUNTIME = ("minor", "nit")
SEVERITY_RANK_RUNTIME = {"blocker": 0, "major": 1, "moderate": 2, "minor": 3, "nit": 4}


def acceptance_check(
    scores_a: List[int],
    scores_b: List[int],
    open_items: List[CritiqueItem] | List[dict],
    threshold: int = 95,
    window: int = 3,
    tolerance: int = 1,
) -> dict:
    """Two-gate acceptance: score gate (strict or stable) + severity gate.
    Returns {accepted, gate, blocking_items, ranked_suggestions, reason}.
    """
    def _sev(c):
        return c.severity if hasattr(c, "severity") else c.get("severity")
    def _addr(c):
        return c.addressed if hasattr(c, "addressed") else c.get("addressed", False)
    def _id(c):
        return c.id if hasattr(c, "id") else c.get("id", "")

    items_open = [c for c in (open_items or []) if not _addr(c)]
    blocking = [c for c in items_open if _sev(c) in BLOCKING_SEVERITIES_RUNTIME]
    suggestions = sorted(
        [c for c in items_open if _sev(c) in SUGGESTION_SEVERITIES_RUNTIME],
        key=lambda c: (SEVERITY_RANK_RUNTIME.get(_sev(c), 99), _id(c)),
    )

    last_a = scores_a[-1] if scores_a else 0
    last_b = scores_b[-1] if scores_b else 0
    strict = last_a >= threshold and last_b >= threshold

    if len(scores_a) >= window and len(scores_b) >= window:
        la, lb = scores_a[-window:], scores_b[-window:]
        avg_ok = (sum(la) / window) >= threshold and (sum(lb) / window) >= threshold
        floor = threshold - tolerance
        floor_ok = all(s >= floor for s in la) and all(s >= floor for s in lb)
        stable = avg_ok and floor_ok
    else:
        stable = False

    if strict:
        gate = "strict"
    elif stable:
        gate = "stable"
    else:
        return {
            "accepted": False, "gate": "",
            "blocking_items": blocking, "ranked_suggestions": suggestions,
            "reason": f"score gate failed (last={last_a},{last_b}; need strict or 3-window stable)",
        }

    if blocking:
        ids = ",".join(_id(c) for c in blocking)
        return {
            "accepted": False, "gate": gate,
            "blocking_items": blocking, "ranked_suggestions": suggestions,
            "reason": f"score gate {gate} OK but {len(blocking)} blocking items open: {ids}",
        }

    return {
        "accepted": True, "gate": gate,
        "blocking_items": [], "ranked_suggestions": suggestions,
        "reason": f"accepted via {gate}; {len(suggestions)} suggestion(s) for user",
    }

