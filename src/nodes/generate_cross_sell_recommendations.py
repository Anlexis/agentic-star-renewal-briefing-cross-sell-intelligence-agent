"""AgentCore Platform v1.0"""

# GenerateCrossSellRecommendationsNode — inner domain node 4: rank the offers.
#
# Turns the detected coverage gaps into a ranked list of recommendations and
# sizes the opportunity. Ranking is deterministic: the renewal risk grade sets
# the starting priority (a HIGH-risk renewal conversation is the one worth
# preparing for), and within a grade the larger indicative premium leads.
#
# The opportunity value is an AGGREGATE — the sum of the indicative annual
# premiums of the recommended covers. Per-category premiums are carried in
# state for ranking but are not part of what the briefing publishes; see
# src/nodes/format_agent_briefing.py for the published figure set.
#
# Indicative premiums are planning figures for the conversation, never quotes.
#
# Inner node — ANONYMOUS trust.

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import finite_int_in_range, from_json, to_json

logger = logging.getLogger(__name__)

_RISK_PRIORITY: Dict[str, int] = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
_DEFAULT_PRIORITY = 3
_MAX_RENDERED_RECOMMENDATIONS = 5

_PREMIUM_MIN = 0
_PREMIUM_MAX = 100_000_000


class GenerateCrossSellRecommendationsNode(FunctionNode):
    """Rank cross-sell recommendations and size the opportunity.

    Input state keys:
        coverage_gaps_json: str
        renewal_risk_json: str

    Output state keys (partial dict):
        cross_sell_json: str
        status: str
        error_log: list[str]  (only on rejection)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        gaps_data: Dict[str, Any] = from_json(state.get("coverage_gaps_json"), {}) or {}
        risk_data: Dict[str, Any] = from_json(state.get("renewal_risk_json"), {}) or {}

        if not gaps_data:
            logger.error("GenerateCrossSellRecommendationsNode: no coverage-gap analysis in state")
            emit_trace_event("cross_sell_failed", {"reason": "missing_gaps"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateCrossSellRecommendationsNode: coverage-gap analysis missing in state"],
            }

        risk_level = str(risk_data.get("risk_level") or "LOW")
        base_priority = _RISK_PRIORITY.get(risk_level, _DEFAULT_PRIORITY)

        priced: List[Dict[str, Any]] = []
        for gap in gaps_data.get("gaps_identified") or []:
            if not isinstance(gap, dict):
                continue
            premium = finite_int_in_range(gap.get("indicative_annual_premium_jpy"), _PREMIUM_MIN, _PREMIUM_MAX)
            if premium is None:
                logger.warning("GenerateCrossSellRecommendationsNode: gap without a usable indicative premium skipped")
                continue
            priced.append({**gap, "indicative_annual_premium_jpy": premium})

        # Larger opportunity first, then by category identifier so the order is
        # stable for identical premiums and the same portfolio always briefs
        # the same way.
        priced.sort(key=lambda item: (-int(item["indicative_annual_premium_jpy"]), str(item.get("category", ""))))

        recommendations: List[Dict[str, Any]] = []
        for offset, gap in enumerate(priced):
            label = str(gap.get("label") or gap.get("category") or "")
            recommendations.append(
                {
                    "rank": base_priority + offset,
                    "category": gap.get("category"),
                    "product_label": label,
                    "indicative_annual_premium_jpy": gap["indicative_annual_premium_jpy"],
                    "rationale": f"{label} is not held in the current portfolio",
                    "talking_point": (
                        f"{label}は現在ご加入がありません。" "更新のタイミングで併せてご検討いただけます。"
                    ),
                }
            )

        opportunity_total = sum(int(item["indicative_annual_premium_jpy"]) for item in recommendations)

        cross_sell = {
            "policy_id": gaps_data.get("policy_id") or risk_data.get("policy_id"),
            "risk_level": risk_level,
            "recommendation_count": len(recommendations),
            "recommendations": recommendations,
            "opportunity_annual_premium_jpy": opportunity_total,
            "rendered_limit": _MAX_RENDERED_RECOMMENDATIONS,
        }

        emit_trace_event(
            "cross_sell_recommendations_generated",
            {"recommendation_count": len(recommendations), "risk_level": risk_level},
            state,
        )
        return {
            "cross_sell_json": to_json(cross_sell),
            "status": AgentStatus.SUCCESS.value,
        }
