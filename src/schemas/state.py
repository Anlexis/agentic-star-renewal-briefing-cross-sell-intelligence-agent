"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only. Do NOT add
# credentials, secrets, or Pydantic models.
#
# Insurance agent renewal briefing & cross-sell intelligence.
# Two-layer nested pipeline: outer backbone (AgentBaseGraph) + inner domain
# workflow (BaseGraph). The fields below cover both layers.
#
# Serialization contract: every dict/list-valued field is stored as a
# JSON-serialized Optional[str]. Use to_json() / from_json() at every producer
# and consumer node — one contract end to end. Never type a dict/list field as
# a bare dict/list; that causes msgpack serialization failures.

from __future__ import annotations

import json
import math
import re
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState

# Caller-supplied strings that reach the briefing are restricted to this inert
# identifier form: lowercase letter first, then lowercase letters, digits and
# underscores. Two properties matter.
#
#   1. Free text from a caller rendered into a generated document is output
#      injection: the caller writes part of the document. An identifier cannot
#      carry markup, instructions or personal data.
#   2. The leading-letter requirement rules out a purely numeric identifier.
#      The output boundary treats long digit runs as monetary figures and snaps
#      them onto the published rounding grid; a caller code of "48210" would be
#      rewritten to "48,000" — the boundary silently renaming the thing the
#      briefing exists to name. Requiring a letter first makes that
#      unrepresentable rather than merely unlikely.
_INERT_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-controlled number: FINITE float within [lo, hi], else None.

    Rejects bools, non-numerics, and — critically — non-finite values. float()
    parses "NaN"/"Infinity" happily, and Python's json accepts bare NaN,
    Infinity and -Infinity in a request body, so both spellings arrive over the
    wire. IEEE NaN comparisons are always False, which turns a bounds check into
    a silent fail-OPEN: a NaN days-to-renewal figure compares below every lapse
    threshold and the briefing reports no renewal risk on exactly the account
    that has one. Every caller-supplied number comes through here.

    Returns the parsed float so the caller can apply its own integrality check.
    None means "reject" — never "substitute a default".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        in_range = lo <= parsed <= hi
    except (TypeError, OverflowError):
        return None
    if not math.isfinite(parsed) or not in_range:
        return None
    return parsed


def finite_int_in_range(value: Any, lo: int, hi: int) -> Optional[int]:
    """Parse a caller-controlled INTEGER: finite, integral, within [lo, hi].

    Wraps finite_in_range() and additionally rejects fractional values, so a
    premium of 84_000.5 is a rejection rather than a truncation that could move
    the figure across a threshold or off the published rounding grid.
    """
    parsed = finite_in_range(value, lo, hi)
    if parsed is None or parsed != int(parsed):
        return None
    return int(parsed)


def inert_identifier(value: Any) -> Optional[str]:
    """Return *value* if it is an inert identifier, else None.

    The single admission rule for every caller string that reaches the
    briefing. Rejection is a rejection: callers never get a sanitized
    approximation of what they sent, because a silently rewritten identifier
    names the wrong policy just as effectively as an injected one.
    """
    if not isinstance(value, str):
        return None
    return value if _INERT_IDENTIFIER_RE.match(value) else None


class State(AgentState):
    """State for the renewal briefing & cross-sell agent.

    All dict/list domain values are stored as Optional[str] (JSON). No
    credential-named fields.

    Every domain field is NotRequired: the TypedDict has to be valid at graph
    initialisation, before any node has written a value. A bare annotation
    (e.g. `validated_input: str`) declares the key mandatory from the first
    LangGraph state construction onwards, which no node can satisfy.
    """

    # ── ValidateInputNode output (outer pre_process) ──────────────────────────
    validated_input: NotRequired[str]  # sanitized agent query
    policy_id: NotRequired[Optional[str]]  # extracted policy identifier

    # ── Runtime policy seeded into the inner graph ───────────────────────────
    # DomainWorkflowGraph._extra_initial_state() resolves config/config.yaml
    # once per invocation and seeds it here, so every inner node decides on the
    # same declared thresholds instead of re-reading the file (or falling back
    # to in-code constants and making the declared values dead).
    runtime_policy_json: NotRequired[Optional[str]]

    # ── Inner domain-node outputs (structured values → JSON string) ───────────
    policy_context_json: NotRequired[Optional[str]]  # AggregateCustomerProfileNode
    renewal_risk_json: NotRequired[Optional[str]]  # IdentifyRenewalRiskNode
    coverage_gaps_json: NotRequired[Optional[str]]  # DetectCoverageGapsNode
    cross_sell_json: NotRequired[Optional[str]]  # GenerateCrossSellRecommendationsNode
    agent_briefing: NotRequired[Optional[str]]  # FormatAgentBriefingNode (final document)

    # ── Outer graph outputs ──────────────────────────────────────────────────
    # result / formatted_output are AgentState backbone fields; they are
    # re-declared here only to narrow the documented type, and stay NotRequired
    # so the re-declaration cannot make a backbone key mandatory.
    result: NotRequired[Optional[str]]  # set by RenewalBriefingGraphNode.merge_output
    formatted_output: NotRequired[Optional[str]]  # released by SecurityGateOutputNode
    error_code: Optional[str]
