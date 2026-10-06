"""AgentCore Platform v1.0"""

# Outer graph for the insurance renewal briefing & cross-sell agent.
#
# Architecture:
#   Outer: RenewalBriefingCrossSellAgent(AgentBaseGraph)
#     pre_process  -> ValidateInputNode        (caller input boundary, VERIFIED_EXTERNAL)
#     main         -> RenewalBriefingGraphNode (GraphNode -> DomainWorkflowGraph)
#     post_process -> SecurityGateOutputNode   (output boundary)
#
#   Inner: DomainWorkflowGraph(BaseGraph) @ src/graph/domain_workflow_graph.py
#     aggregate_customer_profile -> identify_renewal_risk -> detect_coverage_gaps
#     -> generate_cross_sell_recommendations -> format_agent_briefing
#
# Warning: the class name RenewalBriefingCrossSellAgent MUST match:
#    - config/agent.yaml  ->  class: "src.graph.graph.RenewalBriefingCrossSellAgent"
#    - src/api/server.py  ->  from src.graph.graph import RenewalBriefingCrossSellAgent
#
# Warning: add_edges() is NOT overridden — backbone wiring is handled by the framework.
# Warning: inner nodes take NO constructor arguments in register_nodes().

from typing import Any, ClassVar, Dict, Optional

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import set_caller_frame
from src.nodes.security_gate_output import SecurityGateOutputNode
from src.nodes.validate_input import ValidateInputNode
from src.schemas.state import State
from src.services.renewal_policy import load_runtime_config


class RenewalBriefingGraphNode(GraphNode):
    """GraphNode wrapping DomainWorkflowGraph; assigned to the `main` slot.

    Implements the GraphNode contract methods:
      get_subgraph()   -- instantiates DomainWorkflowGraph with the parent config.
      extract_input()  -- passes validated_input to the inner graph AND bridges
                          the caller's input_context across the boundary.
      merge_output()   -- maps the inner sub_result back to the outer state delta.
      _parent_config() -- reads the runtime parameter file and forwards it to
                          the inner graph under config["configurable"].
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> Any:
        """Instantiate the inner domain workflow graph.

        Configuration is passed via self._parent_config(): BaseGraph.__init__
        takes a config argument, and that is the only channel through which the
        declared runtime parameters reach the inner pipeline.
        Imported lazily to avoid a circular import at module load time.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return validated_input for the inner graph, bridging the caller context.

        This hook is the last code that runs on the outer state before
        subgraph.invoke(), which forwards only the input string — so it is also
        the only place the validated caller frame (the context channel and the
        resolved policy identifier) can be handed across. See
        src/graph/context_bridge.py for why the hand-off is needed.
        """
        set_caller_frame(state.get("input_context") or {}, state.get("policy_id"))
        return str(state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph output to the outer state.

        DomainWorkflowGraph.get_output() returns
        {"output": agent_briefing, "status": ..., "node_history": ...}; the
        briefing becomes `result`, which SecurityGateOutputNode then screens.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the runtime parameters to the inner graph.

        Reads config/config.yaml — the runtime-parameter file, as distinct from
        config/agent.yaml, which is the registration manifest and holds no
        tuning values at all — and returns it under config["configurable"],
        where BaseGraph subclasses and their nodes look for tuning values.

        Returning {} here, or reading the manifest instead, makes every
        declared setting dead: the inner graph is built with an empty config,
        runs on in-code defaults, and editing the configuration file has no
        effect at runtime while everything still reports success.

        `timeout_s` is additionally exposed as `timeout_seconds`, the name the
        inner graph validates, so the declared value is not lost to a key-name
        mismatch. A missing or unreadable file degrades to {} rather than
        raising: configuration loading must never become a graph-construction
        failure.
        """
        runtime = load_runtime_config()
        if not runtime:
            return {}
        configurable: Dict[str, Any] = dict(runtime)
        if "timeout_s" in configurable and "timeout_seconds" not in configurable:
            configurable["timeout_seconds"] = configurable["timeout_s"]
        return {"configurable": configurable}


class RenewalBriefingCrossSellAgent(AgentBaseGraph):
    """Outer graph for the insurance agent renewal briefing.

    Fixed backbone: initialize -> pre_process -> main -> post_process -> finalize.
    Domain complexity is encapsulated inside RenewalBriefingGraphNode (`main`).
    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Build the agent, defaulting to the declared runtime parameters.

        A caller that constructs the agent directly — the standalone entry
        point does — otherwise gets an empty config, and the values declared in
        config/config.yaml never reach the graph at all.
        """
        super().__init__(config if config is not None else load_runtime_config())

    @property
    def name(self) -> str:
        return "RenewalBriefingCrossSellAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # initialize + finalize
        self._nodes["pre_process"] = ValidateInputNode()  # caller input boundary
        self._nodes["main"] = RenewalBriefingGraphNode()  # inner graph wrapper
        self._nodes["post_process"] = SecurityGateOutputNode()  # output boundary

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Expose the released briefing — and only on a run that succeeded.

        Two things happen here, and the second is the load-bearing one.

        The briefing is published under the framework-standard 'output' key.
        The inherited accessor returns state.get("output"), which this agent
        never sets: the released text is `formatted_output`, written by the
        output boundary node. Without that mapping, a successful run returns an
        empty output.

        The status check is what makes the boundary's decision stick. The
        inherited accessor resolves the caller-facing document as
        `formatted_output or result` with no reference to the run's status, and
        `result` holds the briefing as the domain pipeline produced it —
        before the boundary screened it. Reproducing that expression here
        publishes exactly the document the boundary refused, one key away from
        a status that says the run failed. So only a SUCCESS run reaches the
        document; every other terminal status returns no document at all,
        whatever the state fields still hold.

        This is deliberately independent of the boundary node clearing those
        fields. Either measure contains the leak alone; together, neither a new
        failure path that forgets to clear nor a future edit to this accessor
        can put an unscreened briefing in front of a caller.
        """
        base: Dict[str, Any] = dict(super().get_output(state))
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return base
        if state.get("status") != AgentStatus.SUCCESS.value:
            base["output"] = None
            return base
        base["output"] = state.get("formatted_output") or state.get("result")
        return base
