"""Declared runtime parameters must actually reach the pipeline.

The failure this file exists to prevent is silent: a graph built with an empty
config runs on in-code defaults, every test passes, and editing
config/config.yaml changes nothing at runtime. Nothing reports an error,
because nothing went wrong — the values were simply never read.

So the assertions here follow one value from the file all the way to the
briefing, and check the boundary conditions of the loader that carries it.
"""

import pytest

from src.graph.context_bridge import get_caller_frame, set_caller_frame
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import RenewalBriefingCrossSellAgent, RenewalBriefingGraphNode
from src.schemas.state import from_json
from src.services.renewal_policy import load_runtime_config, resolve_policy


class TestRuntimeConfigIsRead:
    def test_the_runtime_file_declares_the_policy(self):
        config = load_runtime_config()
        assert config, "config/config.yaml must be readable"
        assert config["renewal_risk_policy"]["policy_version"] == "1.0.0"
        assert config["cross_sell_catalog"]["life"] == 84000
        assert config["timeout_s"] == 30

    def test_the_agent_defaults_to_the_declared_config(self):
        """Constructing the agent with no argument must not yield an empty config."""
        agent = RenewalBriefingCrossSellAgent()
        assert agent.config.get("max_retry") == 3
        assert agent.config.get("timeout_s") == 30

    def test_the_parent_forwards_config_under_configurable(self):
        forwarded = RenewalBriefingGraphNode()._parent_config()
        assert "configurable" in forwarded
        configurable = forwarded["configurable"]
        assert configurable["renewal_risk_policy"]["lapse_days_threshold"] == 30
        # timeout_s is exposed under the name the inner graph validates, so the
        # declared value is not lost to a key-name mismatch.
        assert configurable["timeout_seconds"] == configurable["timeout_s"] == 30

    def test_the_inner_graph_resolves_the_forwarded_policy(self):
        graph = DomainWorkflowGraph(config=RenewalBriefingGraphNode()._parent_config())
        graph._validate_config()
        policy = graph._resolved_policy
        assert policy["policy_version"] == "1.0.0"
        assert policy["source"] == "configuration"
        assert policy["policy_effective"] is True
        assert policy["timeout_seconds"] == 30

    def test_the_resolved_policy_is_seeded_into_the_inner_state(self):
        graph = DomainWorkflowGraph(config=RenewalBriefingGraphNode()._parent_config())
        graph._validate_config()
        set_caller_frame({"customer_ref": "cust_1"}, "POL-880123")
        seeded = graph._extra_initial_state()
        assert seeded["input_context"] == {"customer_ref": "cust_1"}
        assert seeded["policy_id"] == "POL-880123"
        assert from_json(seeded["runtime_policy_json"])["policy_version"] == "1.0.0"


class TestPolicyResolutionBounds:
    """A declared value that cannot be applied is not applied — and says so."""

    def test_absent_configuration_falls_back_and_flags_itself(self):
        policy = resolve_policy({})
        assert policy["policy_effective"] is False
        assert policy["source"] == "template default"
        assert policy["lapse_days_threshold"] == 30

    @pytest.mark.parametrize("value", ["NaN", "Infinity", float("nan"), float("inf"), -1, 10**9])
    def test_unusable_threshold_keeps_the_documented_default(self, value):
        policy = resolve_policy({"renewal_risk_policy": {"lapse_days_threshold": value}})
        assert policy["lapse_days_threshold"] == 30
        assert policy["policy_effective"] is False

    def test_a_usable_threshold_is_applied(self):
        policy = resolve_policy(
            {
                "renewal_risk_policy": {
                    "policy_version": "2.0.0",
                    "lapse_days_threshold": 45,
                    "premium_increase_pct_threshold": 7.5,
                    "claims_count_threshold": 1,
                },
                "cross_sell_catalog": {"life": 90000},
            }
        )
        assert policy["lapse_days_threshold"] == 45
        assert policy["premium_increase_pct_threshold"] == 7.5
        assert policy["policy_version"] == "2.0.0"
        assert policy["policy_effective"] is True

    def test_a_catalog_entry_that_is_not_an_inert_identifier_is_dropped(self):
        policy = resolve_policy({"cross_sell_catalog": {"Life Insurance": 90000, "life": 84000}})
        assert policy["cross_sell_catalog"] == {"life": 84000}
        assert policy["policy_effective"] is False

    @pytest.mark.parametrize("value", ["NaN", float("inf"), -1, 10**12])
    def test_a_catalog_premium_outside_its_range_is_dropped(self, value):
        policy = resolve_policy({"cross_sell_catalog": {"life": value, "auto": 72000}})
        assert "life" not in policy["cross_sell_catalog"]
        assert policy["cross_sell_catalog"]["auto"] == 72000

    def test_an_empty_catalog_falls_back_to_the_template_default(self):
        policy = resolve_policy({"cross_sell_catalog": {}})
        assert set(policy["cross_sell_catalog"]) == {"life", "medical", "accident", "fire", "auto"}


class TestCallerFrameIsolation:
    def test_an_unset_frame_reads_as_empty(self):
        set_caller_frame(None, None)
        frame = get_caller_frame()
        assert frame == {"input_context": {}, "policy_id": None}

    def test_the_frame_copies_rather_than_aliases(self):
        context = {"customer_ref": "cust_1"}
        set_caller_frame(context, "POL-880123")
        context["customer_ref"] = "cust_2"
        assert get_caller_frame()["input_context"]["customer_ref"] == "cust_1"
