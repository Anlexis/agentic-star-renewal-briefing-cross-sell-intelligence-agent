"""AgentCore Platform v1.0"""

# IdentifyRenewalRiskNode — inner domain node 2: grade the renewal risk.
#
# Compares the aggregated profile against the effective thresholds (the agency
# policy from configuration, with any accepted per-request override already
# applied by the profile node) and produces the risk grade the briefing leads
# with, together with the factors that produced it.
#
# The numbers are re-parsed through the finite + bounded parser rather than
# trusted from the serialized profile. State crosses a JSON boundary between
# nodes, and JSON round-trips NaN and Infinity intact; a non-finite figure
# reaching a threshold comparison compares False against every threshold and
# the account is graded LOW — silently, on exactly the decision this node
# exists to make.
#
# Inner node — ANONYMOUS trust.

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.schemas.state import finite_in_range, finite_int_in_range, from_json, to_json

logger = logging.getLogger(__name__)

# Bounds mirror the caller contract in AggregateCustomerProfileNode; re-applied
# here so the comparison inputs are provably finite at the point of decision.
_DAYS_MIN, _DAYS_MAX = 0, 3_650
_PCT_MIN, _PCT_MAX = -100.0, 1_000.0
_CLAIMS_MIN, _CLAIMS_MAX = 0, 1_000
_THRESHOLD_DAYS_MIN, _THRESHOLD_DAYS_MAX = 0, 3_650
_THRESHOLD_PCT_MIN, _THRESHOLD_PCT_MAX = 0.0, 1_000.0
_THRESHOLD_CLAIMS_MIN, _THRESHOLD_CLAIMS_MAX = 0, 1_000

# Documented fallbacks, applied only when a threshold does not survive its
# bounds check. Falling back to a stated value keeps the grading defined;
# leaving the comparison to a non-finite number would not.
_FALLBACK_LAPSE_DAYS = 30
_FALLBACK_PREMIUM_PCT = 10.0
_FALLBACK_CLAIMS = 2

_HIGH_FACTOR_COUNT = 2


class IdentifyRenewalRiskNode(FunctionNode):
    """Grade renewal risk from the aggregated profile.

    Input state keys:
        policy_context_json: str

    Output state keys (partial dict):
        renewal_risk_json: str
        status: str
        error_log: list[str]  (only on rejection)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        profile: Dict[str, Any] = from_json(state.get("policy_context_json"), {}) or {}
        if not profile:
            logger.error("IdentifyRenewalRiskNode: no customer profile in state")
            emit_trace_event("renewal_risk_failed", {"reason": "missing_profile"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["IdentifyRenewalRiskNode: customer profile missing in state"],
            }

        thresholds: Dict[str, Any] = profile.get("risk_thresholds") or {}
        lapse_days = _bounded_int(
            thresholds.get("lapse_days_threshold"), _THRESHOLD_DAYS_MIN, _THRESHOLD_DAYS_MAX, _FALLBACK_LAPSE_DAYS
        )
        premium_pct = _bounded_float(
            thresholds.get("premium_increase_pct_threshold"),
            _THRESHOLD_PCT_MIN,
            _THRESHOLD_PCT_MAX,
            _FALLBACK_PREMIUM_PCT,
        )
        claims_limit = _bounded_int(
            thresholds.get("claims_count_threshold"), _THRESHOLD_CLAIMS_MIN, _THRESHOLD_CLAIMS_MAX, _FALLBACK_CLAIMS
        )

        policy_count = _bounded_int(profile.get("policy_count"), 0, 1_000, 0)
        days_to_renewal = finite_int_in_range(profile.get("days_to_renewal"), _DAYS_MIN, _DAYS_MAX)
        premium_change_pct = finite_in_range(profile.get("premium_change_pct"), _PCT_MIN, _PCT_MAX)
        total_claims = finite_int_in_range(profile.get("total_claims_count"), _CLAIMS_MIN, _CLAIMS_MAX)

        # A figure the caller supplied that does not survive re-parsing here is
        # a rejection, not a zero: the profile node accepted it, so an
        # unparseable value at this point means the pipeline lost integrity
        # between the two, and grading on a substituted number would hide that.
        if profile.get("days_to_renewal") is not None and days_to_renewal is None:
            return _integrity_error(state, "days_to_renewal")
        if profile.get("premium_change_pct") is not None and premium_change_pct is None:
            return _integrity_error(state, "premium_change_pct")
        if profile.get("total_claims_count") is not None and total_claims is None:
            return _integrity_error(state, "total_claims_count")

        factors: List[str] = []
        if policy_count == 0:
            factors.append("有効な契約が登録されていません")
        if days_to_renewal is not None and days_to_renewal <= lapse_days:
            factors.append(f"更新期日まで {days_to_renewal} 日（更新対応期間内）")
        if premium_change_pct is not None and premium_change_pct >= premium_pct:
            factors.append(f"更新時の保険料変動 {premium_change_pct:.1f}%（しきい値 {premium_pct:.1f}% 以上）")
        if total_claims is not None and total_claims >= claims_limit:
            factors.append(f"当期の請求件数 {total_claims} 件（しきい値 {claims_limit} 件以上）")

        if policy_count == 0 or len(factors) >= _HIGH_FACTOR_COUNT:
            risk_level = "HIGH"
        elif factors:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        renewal_risk = {
            "policy_id": profile.get("policy_id"),
            "risk_level": risk_level,
            "risk_factors": factors,
            "applied_thresholds": {
                "lapse_days_threshold": lapse_days,
                "premium_increase_pct_threshold": premium_pct,
                "claims_count_threshold": claims_limit,
            },
            "factor_count": len(factors),
        }

        emit_trace_event(
            "renewal_risk_identified",
            {"risk_level": risk_level, "factor_count": len(factors)},
            state,
        )
        return {
            "renewal_risk_json": to_json(renewal_risk),
            "status": AgentStatus.SUCCESS.value,
        }


def _bounded_int(value: Any, lo: int, hi: int, fallback: int) -> int:
    """Re-parse an integer at the point of decision, falling back if unusable."""
    parsed = finite_int_in_range(value, lo, hi)
    if parsed is None:
        logger.warning("IdentifyRenewalRiskNode: threshold outside its accepted range; documented default applied")
        return fallback
    return parsed


def _bounded_float(value: Any, lo: float, hi: float, fallback: float) -> float:
    """Re-parse a decimal at the point of decision, falling back if unusable."""
    parsed = finite_in_range(value, lo, hi)
    if parsed is None:
        logger.warning("IdentifyRenewalRiskNode: threshold outside its accepted range; documented default applied")
        return fallback
    return parsed


def _integrity_error(state: AgentState, field: str) -> Dict[str, Any]:
    """Fail closed when a validated figure no longer parses. Names the field only."""
    logger.error("IdentifyRenewalRiskNode: %s is not a finite in-range value at the point of decision", field)
    emit_trace_event("renewal_risk_failed", {"reason": "unbounded_figure", "field": field}, state)
    emit_progress(INPUT_REJECTED)
    return {
        "status": AgentStatus.SUCCESS.value,
        "error_code": "INVALID_REQUEST",
        "error_log": [f"IdentifyRenewalRiskNode: {field} must be a finite value within the documented range"],
    }
