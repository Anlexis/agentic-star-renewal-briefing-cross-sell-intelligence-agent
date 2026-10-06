# Unit tests: the caller trust gate.
#
# These tests invoke nodes via node(state) — through BaseNode.__call__, which
# runs the trust gate, then the framework input screen, then execute(), then
# the framework output screen. A test that calls node.execute(state) directly
# bypasses __call__ and never exercises the gate at all.
#
# A denial RETURNS an error dict (it never raises): status ERROR, an error_log
# entry naming the node and the required level, and — because execute() never
# ran — none of the node's own output keys.
#
# Every assertion below is pinned to an artefact of the node under test (its own
# output keys, its own error prefix, the required level named in the denial), so
# that lowering required_trust_level on the gate node makes these tests fail
# rather than pass for an unrelated reason.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.aggregate_customer_profile import AggregateCustomerProfileNode
from src.nodes.detect_coverage_gaps import DetectCoverageGapsNode
from src.nodes.format_agent_briefing import FormatAgentBriefingNode
from src.nodes.generate_cross_sell_recommendations import GenerateCrossSellRecommendationsNode
from src.nodes.identify_renewal_risk import IdentifyRenewalRiskNode
from src.nodes.security_gate_output import WITHHELD_NOTICE, SecurityGateOutputNode
from src.nodes.validate_input import ValidateInputNode

# A clean, valid renewal-briefing request. Deliberately free of anything the
# framework's own screens or this node's input screens would touch, so that the
# ONLY thing that can turn this input into an error is the trust gate.
_VALID_INPUT = "契約更新の準備状況を確認したい"


def _state(trust_value: str, **extra) -> dict:
    state = {
        "user_input": _VALID_INPUT,
        "input_context": {},
        "caller_trust_level": trust_value,
        "node_history": [],
        "error_log": [],
        "session_id": "test-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestTrustGate:
    """The trust gate — every invocation goes through node(state) / __call__."""

    def test_anonymous_caller_denied_on_validate_input(self):
        """ANONYMOUS caller against the VERIFIED_EXTERNAL pre_process node.

        __call__ must RETURN (never raise) an error dict, and execute() must not
        have run — so validated_input / policy_id, the only two keys this node
        ever produces, are ABSENT.
        """
        node = ValidateInputNode()  # required_trust_level = VERIFIED_EXTERNAL
        result = node(_state(TrustLevel.ANONYMOUS.value))

        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            "trust gate denied" in str(e) for e in error_log
        ), f"expected a trust denial in error_log, got: {error_log}"
        # Pin the denial to THIS node and THIS required level: if
        # ValidateInputNode.required_trust_level is lowered, no denial is
        # produced at all and both assertions below fail.
        assert any("ValidateInputNode" in str(e) for e in error_log)
        assert any(
            "required=VERIFIED_EXTERNAL" in str(e) for e in error_log
        ), f"denial must name the required level; got: {error_log}"
        assert "validated_input" not in result, "execute() must not run on a trust denial — validated_input leaked"
        assert "policy_id" not in result, "execute() must not run on a trust denial — policy_id leaked"

    def test_verified_external_caller_passes_validate_input(self):
        """A VERIFIED_EXTERNAL caller clears the gate and the node runs.

        This is the other half of the assertion: the same input that is
        refused above must succeed here, which proves the refusal came from the
        trust gate and not from the node's own input validation.
        """
        node = ValidateInputNode()
        result = node(_state(TrustLevel.VERIFIED_EXTERNAL.value))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("validated_input") == _VALID_INPUT
        assert not any("trust gate denied" in str(e) for e in result.get("error_log", []))

    def test_internal_caller_passes_validate_input(self):
        """INTERNAL outranks VERIFIED_EXTERNAL, so it clears the same gate."""
        node = ValidateInputNode()
        result = node(_state(TrustLevel.INTERNAL.value))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("validated_input") == _VALID_INPUT

    def test_missing_trust_level_defaults_to_anonymous_and_is_denied(self):
        """Secure-by-default: no caller_trust_level in state is read as ANONYMOUS."""
        state = _state(TrustLevel.ANONYMOUS.value)
        del state["caller_trust_level"]
        result = ValidateInputNode()(state)

        assert result.get("status") == AgentStatus.ERROR.value
        assert any("trust gate denied" in str(e) for e in result.get("error_log", []))
        assert "validated_input" not in result

    def test_anonymous_caller_allowed_on_inner_node(self):
        """Inner domain nodes are ANONYMOUS by design and must admit that caller.

        Asserts on an artefact only this node writes (policy_context_json), so a
        pass here means the node really ran rather than merely not being denied.
        """
        node = AggregateCustomerProfileNode()
        result = node(_state(TrustLevel.ANONYMOUS.value, policy_id="INS-TEST01"))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("policy_context_json"), "inner node should produce a profile"
        assert not any("trust gate denied" in str(e) for e in result.get("error_log", []))

    def test_anonymous_caller_allowed_on_output_gate(self):
        """The post_process output boundary is ANONYMOUS and must admit that caller."""
        briefing = "# 更新ブリーフィング\n契約内容を確認しました。"
        result = SecurityGateOutputNode()(_state(TrustLevel.ANONYMOUS.value, result=briefing))

        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("formatted_output") == briefing


class TestOutputBoundaryIsThisNodesGate:
    """The rejection must come from SecurityGateOutputNode's own boundary.

    The framework applies its own credential scan to every node's result, so
    asserting only `status == ERROR` on credential-shaped output would pass
    even if this node had no boundary of its own. These cases use a script
    fragment — which the framework scan does not look for — and assert this
    node's own "SecurityGateOutputNode:" prefix.
    """

    def test_script_fragment_blocked_by_this_nodes_gate(self):
        result = SecurityGateOutputNode()(
            _state(TrustLevel.ANONYMOUS.value, result="ブリーフィング<script>alert(1)</script>")
        )

        assert result.get("status") == AgentStatus.ERROR.value
        error_log = result.get("error_log", [])
        assert any(
            str(e).startswith("SecurityGateOutputNode:") for e in error_log
        ), f"expected this node's own rejection, got: {error_log}"
        assert result.get("formatted_output") == WITHHELD_NOTICE, "blocked output must not be released"
        assert "<script>" not in repr(result)

    def test_oversize_output_blocked_by_this_nodes_gate(self):
        result = SecurityGateOutputNode()(_state(TrustLevel.ANONYMOUS.value, result="あ" * 16_001))

        assert result.get("status") == AgentStatus.ERROR.value
        assert any(
            str(e).startswith("SecurityGateOutputNode:") and "oversize_output" in str(e)
            for e in result.get("error_log", [])
        )
        assert result.get("formatted_output") == WITHHELD_NOTICE


class TestTrustLevelMatrix:
    """The template's declared trust matrix (docs/02_design.md).

    The outer entry slot requires VERIFIED_EXTERNAL; the output slot and the
    five inner domain nodes run behind that boundary and are ANONYMOUS per the
    nested-graph convention (a higher level on an inner node would be denied at
    runtime, since GraphNode propagates the caller's level unescalated).
    """

    def test_entry_node_requires_verified_external(self):
        assert ValidateInputNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_inner_and_output_nodes_admit_anonymous(self):
        for node_cls in (
            AggregateCustomerProfileNode,
            IdentifyRenewalRiskNode,
            DetectCoverageGapsNode,
            GenerateCrossSellRecommendationsNode,
            FormatAgentBriefingNode,
            SecurityGateOutputNode,
        ):
            assert node_cls.required_trust_level is TrustLevel.ANONYMOUS, (
                f"{node_cls.__name__} must declare TrustLevel.ANONYMOUS " "(runs behind the outer caller boundary)"
            )
