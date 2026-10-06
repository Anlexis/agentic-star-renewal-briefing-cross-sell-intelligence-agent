"""AgentCore Platform v1.0"""

# DomainWorkflowGraph — the inner BaseGraph of the nested pipeline.
#
# Instantiated by RenewalBriefingGraphNode.get_subgraph() in src/graph/graph.py.
# Inherits BaseGraph: a fully custom topology with no forced backbone slots.
#
# Pipeline (linear):
#   START
#     -> aggregate_customer_profile  (validate the caller contract; portfolio totals)
#     -> identify_renewal_risk       (risk grade + factors, against the declared policy)
#     -> detect_coverage_gaps        (portfolio cover vs. the configured catalog)
#     -> generate_cross_sell_recommendations  (ranked recommendations + opportunity value)
#     -> format_agent_briefing       (the agent-ready briefing document)
#   -> END
#
# Warning: register_nodes() does NOT call super() — BaseGraph.register_nodes()
# is abstract.
# Warning: every domain node is instantiated with NO constructor arguments —
# nodes are stateless.
# Warning: inner nodes declare ANONYMOUS trust; the caller boundary is the
# outer pre_process node.

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import get_caller_frame
from src.nodes.aggregate_customer_profile import AggregateCustomerProfileNode
from src.nodes.detect_coverage_gaps import DetectCoverageGapsNode
from src.nodes.format_agent_briefing import FormatAgentBriefingNode
from src.nodes.generate_cross_sell_recommendations import GenerateCrossSellRecommendationsNode
from src.nodes.identify_renewal_risk import IdentifyRenewalRiskNode
from src.schemas.state import State, to_json
from src.services.renewal_policy import resolve_policy


class DomainWorkflowGraph(BaseGraph):
    """Inner graph: the five-step renewal briefing pipeline.

    get_output() is designed together with merge_output() in src/graph/graph.py.
    """

    # Resolved once per graph construction by _validate_config().
    _resolved_policy: Dict[str, Any]

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "renewal_briefing_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Check the forwarded runtime parameters without failing construction.

        The declared values are advisory tuning inputs, so an absent or
        out-of-range one falls back to the documented default rather than
        refusing to build the graph. What must not happen silently is the
        opposite case — configuration that was declared, forwarded and then
        ignored — so resolve_policy() records provenance and the briefing
        states which policy it applied.
        """
        self._resolved_policy = resolve_policy(self._configurable())

    def _configurable(self) -> Dict[str, Any]:
        """Return the tuning mapping the parent forwarded, or {}."""
        configurable = (self.config or {}).get("configurable")
        return configurable if isinstance(configurable, dict) else {}

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the caller's context and the resolved policy into the inner state.

        Two things cross into this graph here. The validated caller frame,
        which the GraphNode boundary does not forward on its own (see
        src/graph/context_bridge.py), and the resolved runtime policy, so every
        node in the pipeline decides against one consistent set of thresholds
        resolved once per invocation.
        """
        policy = getattr(self, "_resolved_policy", None) or resolve_policy(self._configurable())
        frame = get_caller_frame()
        return {
            "input_context": frame.get("input_context") or {},
            "policy_id": frame.get("policy_id"),
            "runtime_policy_json": to_json(policy),
        }

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register the domain nodes with NO constructor arguments.

        Do NOT call super() — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize/finalize — those are outer backbone concerns.
        """
        self._nodes["aggregate_customer_profile"] = AggregateCustomerProfileNode()
        self._nodes["identify_renewal_risk"] = IdentifyRenewalRiskNode()
        self._nodes["detect_coverage_gaps"] = DetectCoverageGapsNode()
        self._nodes["generate_cross_sell_recommendations"] = GenerateCrossSellRecommendationsNode()
        self._nodes["format_agent_briefing"] = FormatAgentBriefingNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Linear pipeline: profile -> risk -> gaps -> cross-sell -> briefing."""
        self._sg.add_edge(START, "aggregate_customer_profile")
        self._sg.add_edge("aggregate_customer_profile", "identify_renewal_risk")
        self._sg.add_edge("identify_renewal_risk", "detect_coverage_gaps")
        self._sg.add_edge("detect_coverage_gaps", "generate_cross_sell_recommendations")
        self._sg.add_edge("generate_cross_sell_recommendations", "format_agent_briefing")
        self._sg.add_edge("format_agent_briefing", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph contract. Linear topology: never called at runtime."""
        return END if state.get("status") == AgentStatus.ERROR.value else "format_agent_briefing"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the sub_result handed to RenewalBriefingGraphNode.merge_output().

        Designed together with merge_output() in src/graph/graph.py:
          inner get_output() -> {"output": agent_briefing, "status": ...}
          outer merge_output() reads sub_result["output"] -> sets "result"
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "output": state.get("agent_briefing"),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
        }
