"""AgentCore Platform v1.0"""

# Registry entry point — what makes this agent discoverable.
#
# config/agent.yaml declares:
#     module: "src.graph"
#     class:  "RenewalBriefingCrossSellAgent"
# and AgentRegistry resolves that pair with
#     getattr(import_module("src.graph"), "RenewalBriefingCrossSellAgent")
#
# Re-exporting the class here is what makes that getattr succeed. Without it the
# package still imports cleanly and CI still passes — pytest and compileall only
# ever import src.graph.graph directly — but a real registry load raises
# AttributeError, so the failure only surfaces at deploy time.

from .graph import RenewalBriefingCrossSellAgent

__all__ = ["RenewalBriefingCrossSellAgent"]
