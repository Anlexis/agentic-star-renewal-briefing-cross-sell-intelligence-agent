"""AgentCore Platform v1.0"""

# DetectCoverageGapsNode — inner domain node 3: compare cover against catalog.
#
# The reference catalog is the configured cross-sell catalog resolved for this
# invocation (config/config.yaml -> cross_sell_catalog), not a list hard-coded
# here: an agency adds or drops a product line by editing configuration.
#
# A "gap" is a catalog category the portfolio does not already hold. Category
# keys are inert identifiers validated upstream, so they are safe to render;
# the display labels below are a presentation convenience for the categories
# this template ships with, and an unrecognised category renders under its own
# identifier rather than being dropped from the briefing.
#
# Inner node — ANONYMOUS trust.

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import finite_int_in_range, from_json, inert_identifier, to_json

logger = logging.getLogger(__name__)

_CATEGORY_LABELS: Dict[str, str] = {
    "life": "生命保険",
    "medical": "医療保険",
    "accident": "傷害保険",
    "fire": "火災保険",
    "auto": "自動車保険",
}

_CATALOG_PREMIUM_MIN = 0
_CATALOG_PREMIUM_MAX = 100_000_000


def category_label(category: str) -> str:
    """Display label for a cover category; the identifier itself when unknown."""
    return _CATEGORY_LABELS.get(category, category)


class DetectCoverageGapsNode(FunctionNode):
    """Identify catalog categories the portfolio does not hold.

    Input state keys:
        policy_context_json: str

    Output state keys (partial dict):
        coverage_gaps_json: str
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
        profile: Dict[str, Any] = from_json(state.get("policy_context_json"), {}) or {}
        if not profile:
            logger.error("DetectCoverageGapsNode: no customer profile in state")
            emit_trace_event("coverage_gaps_failed", {"reason": "missing_profile"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DetectCoverageGapsNode: customer profile missing in state"],
            }

        catalog_raw = profile.get("cross_sell_catalog") or {}
        covered = {str(c) for c in (profile.get("covered_categories") or [])}

        gaps: List[Dict[str, Any]] = []
        for key, premium_raw in sorted(catalog_raw.items()):
            category = inert_identifier(key)
            premium = finite_int_in_range(premium_raw, _CATALOG_PREMIUM_MIN, _CATALOG_PREMIUM_MAX)
            if category is None or premium is None:
                # The catalog is resolved and bounds-checked before it reaches
                # this node, so an entry that fails here is a defect rather
                # than a caller mistake. Skipping it keeps the briefing honest
                # about the categories it could actually price.
                logger.warning("DetectCoverageGapsNode: catalog entry skipped (key=%r)", str(key)[:32])
                continue
            if category in covered:
                continue
            gaps.append(
                {
                    "category": category,
                    "label": category_label(category),
                    "indicative_annual_premium_jpy": premium,
                }
            )

        coverage_gaps = {
            "policy_id": profile.get("policy_id"),
            "current_coverage": sorted(covered),
            "gaps_identified": gaps,
            "gap_count": len(gaps),
            "catalog_size": len(catalog_raw),
        }

        emit_trace_event(
            "coverage_gaps_detected",
            {"gap_count": len(gaps), "catalog_size": len(catalog_raw)},
            state,
        )
        return {
            "coverage_gaps_json": to_json(coverage_gaps),
            "status": AgentStatus.SUCCESS.value,
        }
