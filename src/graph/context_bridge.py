"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the caller's input_context across the
# outer -> inner graph boundary.
#
# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)`, without forwarding
# the outer state's input_context. Only the user_input string crosses. An
# inner node reading state["input_context"] would therefore always see {}, so
# the whole caller-data contract — the portfolio, the renewal calendar, the
# threshold overrides — would be invisible to the pipeline that exists to
# analyse it, and the briefing would silently fall back to its empty-portfolio
# baseline for every request.
#
# The two sanctioned subclass hooks bridge it:
#
#   RenewalBriefingGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       -> set_caller_frame(state["input_context"], state["policy_id"])
#   DomainWorkflowGraph._extra_initial_state()     [runs INSIDE subgraph.invoke]
#       -> returns get_caller_frame()
#
# The frame carries the resolved policy identifier alongside the context for
# the same reason: it is produced by the outer validation node and would
# otherwise be lost at the boundary, leaving every briefing unable to name the
# policy it is about.
#
# A ContextVar keeps the hand-off correct per thread and per task, so
# concurrent invocations in one process cannot read each other's context.

from contextvars import ContextVar
from typing import Any, Optional

_CALLER_FRAME: ContextVar[Optional[dict[str, Any]]] = ContextVar("renewal_briefing_caller_frame", default=None)


def set_caller_frame(input_context: Optional[dict[str, Any]], policy_id: Optional[str] = None) -> None:
    """Stash the outer validated caller frame for the imminent inner-graph invoke."""
    _CALLER_FRAME.set(
        {
            "input_context": dict(input_context) if input_context else {},
            "policy_id": policy_id,
        }
    )


def get_caller_frame() -> dict[str, Any]:
    """Read (without consuming) the stashed frame; an empty frame when none was set."""
    return _CALLER_FRAME.get() or {"input_context": {}, "policy_id": None}
