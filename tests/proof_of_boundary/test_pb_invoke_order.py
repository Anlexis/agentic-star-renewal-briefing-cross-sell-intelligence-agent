# PB-6: Invoke Execution Order Verification — INS-C2-018
#
# Two complementary PB-6 tests:
#
#   TestBackboneInvokeOrder  — full Graph().invoke() over a SUCCESS-yielding payload,
#                              asserting that the backbone runs and returns a valid result.
#                              Uses _MAIN_SLOT_NODE and _VALID_PAYLOAD (template-specific).
#
#   TestInvokeOrder          — per-node __call__ call-order check (generic, discovers
#                              all concrete BaseNode subclasses under src/nodes/).
#                              Verifies the framework contract:
#                              trust gate -> node_start -> input screen ->
#                              execute() -> output screen -> node_complete.

import importlib
import inspect
import pkgutil

import pytest

from src.graph.graph import RenewalBriefingCrossSellAgent, RenewalBriefingGraphNode

# -- Template-specific constants -----------------------------------------------

_MAIN_SLOT_NODE = RenewalBriefingGraphNode

# A SUCCESS-yielding payload: non-empty, clean, within the length limit. Kept
# byte-identical to deploy/invoke_payload.json so the shipped sample request and
# the one this test proves are the same request.
_VALID_PAYLOAD = "契約 POL-880123 の更新ブリーフィングを作成してください"


# -- PB-6 backbone invoke test --------------------------------------------------


class TestBackboneInvokeOrder:
    """PB-6 (backbone): full Graph().invoke() asserts SUCCESS path and correct main slot.

    _VALID_PAYLOAD must yield AgentStatus.SUCCESS through the full pipeline so that
    all backbone slots (pre_process -> main -> post_process) execute in order.
    A non-SUCCESS result short-circuits post_process, making this test non-trivially
    load-bearing.

    Trust level: TrustLevel.VERIFIED_EXTERNAL reflects the real invocation path
    (authenticated caller → outer ValidateInputNode → inner domain pipeline). Inner
    domain nodes declare required_trust_level = TrustLevel.ANONYMOUS: the caller
    boundary lives on ValidateInputNode, and inner nodes run behind it.
    """

    def test_backbone_invoke_returns_success_and_output(self):
        """Full pipeline invocation with VERIFIED_EXTERNAL trust yields non-empty output."""
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel

        agent = RenewalBriefingCrossSellAgent()
        agent.compile()

        # VERIFIED_EXTERNAL reflects the real caller. Inner nodes use
        # TrustLevel.ANONYMOUS so they pass regardless of caller trust level — the
        # caller boundary lives on the pre_process slot only.
        ctx = InvocationContext(
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            session_id="pb6-backbone-test",
        )
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        # agent.invoke() returns AgentBaseGraph.get_output(state); our override
        # maps formatted_output -> output.
        output = result.get("output")
        assert output, "Expected non-empty 'output' key from agent.invoke(); " f"result: {result}"

        # Status must be SUCCESS on the happy path.
        status = result.get("status")
        expected = (AgentStatus.SUCCESS, AgentStatus.SUCCESS.value, "SUCCESS")
        assert status in expected, (
            f"Expected SUCCESS status; got: {status!r}. " f"error_log: {result.get('error_log', [])}"
        )

    def test_main_slot_node_is_graph_node_subclass(self):
        """_MAIN_SLOT_NODE must be a GraphNode subclass (Cat-2 contract)."""
        from framework.nodes.graph_node import GraphNode

        assert issubclass(
            _MAIN_SLOT_NODE, GraphNode
        ), f"{_MAIN_SLOT_NODE.__name__} must inherit GraphNode to act as the main slot"

    def test_main_slot_registered_correctly(self):
        """The compiled agent's main slot must be an instance of _MAIN_SLOT_NODE."""
        agent = RenewalBriefingCrossSellAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert isinstance(main_node, _MAIN_SLOT_NODE), (
            f"Expected main slot to be {_MAIN_SLOT_NODE.__name__}; " f"got: {type(main_node).__name__}"
        )


# -- PB-6 per-node call-order test (generic -- discovers all src/nodes/*.py) ---


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """Per-node: __call__ runs trust gate -> node_start -> input screen -> execute()
    -> output screen -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)
