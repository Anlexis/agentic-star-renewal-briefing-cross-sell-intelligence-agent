"""AgentCore Platform v1.0"""

# ValidateInputNode — the outer pre_process slot: the caller boundary.
#
# Responsibilities:
#   - Validate and sanitize the agent's query text (instruction-override and
#     prompt-disclosure screens, length limit).
#   - Apply the same screens to the CONTEXT CHANNEL. The framework's own input
#     screen looks at user_input only, so text smuggled into input_context
#     reaches the pipeline unexamined unless the template checks it. This node
#     checks it.
#   - Extract and strictly validate the policy identifier, which is rendered
#     into the briefing and therefore may not be free text.
#
# No external calls. No credentials.

import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

_MAX_INPUT_LEN = 4096

# Depth and breadth caps for the context-channel walk. A caller controls the
# shape of input_context as well as its contents, so the traversal is bounded
# before it starts rather than trusted to terminate.
_MAX_CONTEXT_DEPTH = 6
_MAX_CONTEXT_NODES = 500

# Instruction-override and prompt-disclosure screens.
#
# Both ends of every pattern are anchored on word boundaries and every pattern
# requires a complete phrase, never a bare fragment. That distinction is the
# difference between a screen and an outage: an unanchored fragment matches
# ordinary domain sentences — an insurance query legitimately says "act as a
# settlement agent" — and a validator that refuses real work is a worse
# failure than one that lets a clumsy attack through to the next layer.
#
# The temporal forms ("ignore previous", "disregard the above") stay
# object-free on purpose: the attack idiom is the temporal reference itself,
# and an attacker does not have to name what they are overriding.
_INJECTION_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # instruction override, temporal object: "ignore all previous", "disregard the above"
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
        r"(?:previous|prior|earlier|above|preceding|foregoing)\b",
        re.I,
    ),
    # instruction override, named object: "override your instructions"
    re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
        r"(?:instruction|prompt|rule|direction|guideline|constraint)s?\b",
        re.I,
    ),
    # prompt disclosure
    re.compile(
        r"\b(?:reveal|show|print|output|repeat|display|dump)\s+(?:me\s+)?(?:the\s+|your\s+)*"
        r"(?:system\s*prompt|initial\s*prompt|system\s*message|hidden\s*instruction)s?\b",
        re.I,
    ),
    re.compile(r"\bsystem\s*prompt\b", re.I),
    # role reassignment
    re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b", re.I),
    # conversation-delimiter and markup injection
    re.compile(r"</?\s*(?:system|assistant|user)\s*>", re.I),
    re.compile(r"<\s*script[\s>/]", re.I),
    re.compile(r"\{\{[^}]*\}\}"),
)

# Contact identifiers must not travel on the context channel: this agent needs
# a portfolio, not a way to reach the policyholder, and an address or phone
# number in a briefing is personal data the template never had a reason to
# hold. Both patterns require a complete, well-formed identifier — a date
# (2026-07-01) and a policy number (POL-123456) do not match either.
_CONTACT_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b\d{2,4}-\d{2,4}-\d{4}\b"),
)

# The policy identifier is rendered into the briefing, so it is admitted only
# in its two documented forms and matched end to end. A partial match would let
# arbitrary surrounding text ride into the document.
_POLICY_ID_RE = re.compile(r"(?:INS-[0-9A-Z]{4,12}|POL-[0-9]{6,12})", re.I)
_POLICY_ID_EXACT_RE = re.compile(r"\A(?:INS-[0-9A-Z]{4,12}|POL-[0-9]{6,12})\Z", re.I)


def _screen_text(text: str) -> Optional[str]:
    """Return a reason string when *text* trips a screen, else None."""
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return "input carries an instruction-override or prompt-disclosure pattern"
    for pattern in _CONTACT_PATTERNS:
        if pattern.search(text):
            return "input carries a contact identifier, which this agent does not accept"
    return None


def _validate_user_input(text: str) -> Tuple[bool, str]:
    """Check the agent's query text. Returns (ok, reason); reason is "" when ok.

    The reason names the class of problem and never quotes the input: an error
    message is an output surface too, and echoing rejected text puts the
    payload straight back into the log.
    """
    if not text or not text.strip():
        return False, "user_input is empty"
    if len(text) > _MAX_INPUT_LEN:
        return False, f"user_input exceeds {_MAX_INPUT_LEN} chars"
    reason = _screen_text(text)
    if reason:
        return False, reason
    return True, ""


def _screen_context(
    context: Any, path: str = "input_context", depth: int = 0, budget: Optional[List[int]] = None
) -> Optional[str]:
    """Apply the text screens to every string in the context channel.

    Returns a reason naming the FIELD PATH that failed, or None when the whole
    structure is clean. The traversal is bounded in depth and in node count;
    exceeding either bound is itself a rejection, because an unbounded walk
    over caller-shaped data is a denial-of-service surface.
    """
    if budget is None:
        budget = [_MAX_CONTEXT_NODES]
    if depth > _MAX_CONTEXT_DEPTH:
        return f"{path}: nesting exceeds the accepted depth of {_MAX_CONTEXT_DEPTH}"
    budget[0] -= 1
    if budget[0] < 0:
        return f"input_context exceeds the accepted size of {_MAX_CONTEXT_NODES} entries"

    if isinstance(context, str):
        reason = _screen_text(context)
        return f"{path}: {reason}" if reason else None
    if isinstance(context, dict):
        for key, value in context.items():
            child = _screen_context(value, f"{path}.{str(key)[:32]}", depth + 1, budget)
            if child:
                return child
        return None
    if isinstance(context, (list, tuple)):
        for index, value in enumerate(context):
            child = _screen_context(value, f"{path}[{index}]", depth + 1, budget)
            if child:
                return child
        return None
    return None


def _extract_policy_id(text: str, context: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the policy identifier. Returns (policy_id, reason).

    A declared identifier in the context channel wins over one parsed out of
    the query text, but only if it matches a documented form end to end. A
    malformed declaration is a rejection rather than a fallback to the parsed
    value: quietly briefing on a different policy than the caller named is the
    worse outcome.
    """
    declared = context.get("policy_id")
    if declared is not None:
        if not isinstance(declared, str) or not _POLICY_ID_EXACT_RE.match(declared.strip()):
            return None, "input_context.policy_id is not a recognised policy identifier"
        return declared.strip().upper(), None
    match = _POLICY_ID_RE.search(text)
    return (match.group(0).upper() if match else None), None


# Instruction-override content is refused outright: it is not a value the caller
# can correct by rewording, so that run terminates. Everything else the screens
# report — an empty request, a length overrun, a field outside the documented
# contract — is correctable, so the run completes carrying the reason.
_TERMINATES = "instruction-override"


def _reason_code(reason: str) -> str:
    """Map a screen reason to the caller-facing code, or "" to terminate."""
    if _TERMINATES in reason:
        return ""
    if "is empty" in reason:
        return "EMPTY_INPUT"
    if "exceeds" in reason:
        return "QUESTION_TOO_LONG"
    return "INVALID_REQUEST"


def _declined(reason: str) -> Dict[str, Any]:
    """Build the outcome for a screen that rejected the request."""
    code = _reason_code(reason)
    if code:
        emit_progress(INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [f"ValidateInputNode: {reason}"],
        }
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"ValidateInputNode: {reason}"],
    }


class ValidateInputNode(FunctionNode):
    """The caller boundary: screens the query and the context channel.

    Sets validated_input and policy_id. A rejection returns an error status and
    none of this node's output keys, so nothing unvalidated reaches the domain
    pipeline.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input: str = state.get("user_input", "") or ""
        input_context: Dict[str, Any] = state.get("input_context", {}) or {}

        ok, reason = _validate_user_input(user_input)
        if not ok:
            emit_trace_event("input_rejected", {"surface": "user_input"}, state)
            return _declined(reason)

        context_reason = _screen_context(input_context)
        if context_reason:
            emit_trace_event("input_rejected", {"surface": "input_context"}, state)
            return _declined(context_reason)

        policy_id, policy_reason = _extract_policy_id(user_input, input_context)
        if policy_reason:
            emit_trace_event("input_rejected", {"surface": "policy_id"}, state)
            return _declined(policy_reason)

        emit_trace_event(
            "input_validated",
            {
                "input_chars": len(user_input.strip()),
                "policy_id_found": policy_id is not None,
            },
            state,
        )
        return {
            "validated_input": user_input.strip(),
            "policy_id": policy_id,
            "status": AgentStatus.SUCCESS.value,
        }
