"""AgentCore Platform v1.0"""

# AggregateCustomerProfileNode — inner domain node 1: the caller-data contract.
#
# This node owns the boundary between what a caller sends and what the pipeline
# is allowed to compute on. It validates every field of input_context against
# explicit bounds, aggregates the portfolio, and writes the profile the rest of
# the pipeline reads. Nothing downstream re-derives a value from raw caller
# data, so this is the only place the contract has to hold.
#
# Contract (all fields optional; absent data degrades to the empty-portfolio
# baseline, present-but-invalid data is REJECTED):
#
#   customer_ref            inert identifier, rendered into the briefing
#   portfolio.policies[]    at most 50 entries, each:
#                             category            inert identifier
#                             annual_premium_jpy  integer, 0 .. 100,000,000
#                             claims_count        integer, 0 .. 1,000
#   renewal_calendar.days_to_renewal      integer, 0 .. 3,650
#   renewal_calendar.premium_change_pct   number, -100 .. 1,000
#   risk_policy_overrides.*               per-request threshold overrides,
#                                         same bounds as the configured policy
#
# Rejection is by field NAME. The offending value is never echoed: an error log
# is a place caller-controlled text ends up being read, and repeating a payload
# there just moves it rather than stopping it.
#
# Inner node — ANONYMOUS trust; the caller boundary is the outer pre_process slot.

import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.schemas.state import (
    finite_in_range,
    finite_int_in_range,
    from_json,
    inert_identifier,
    to_json,
)
from src.services.renewal_policy import resolve_policy

logger = logging.getLogger(__name__)

_MAX_POLICIES = 50
_PREMIUM_MIN = 0
_PREMIUM_MAX = 100_000_000
_CLAIMS_MIN = 0
_CLAIMS_MAX = 1_000
_DAYS_MIN = 0
_DAYS_MAX = 3_650
_PCT_MIN = -100.0
_PCT_MAX = 1_000.0

# Threshold overrides accept the same ranges as the configured policy, so a
# per-request override cannot reach a value an operator could not configure.
_OVERRIDE_INT_BOUNDS: Dict[str, Tuple[int, int]] = {
    "lapse_days_threshold": (0, 3_650),
    "claims_count_threshold": (0, 1_000),
}
_OVERRIDE_FLOAT_BOUNDS: Dict[str, Tuple[float, float]] = {
    "premium_increase_pct_threshold": (0.0, 1_000.0),
}


class _Rejected(Exception):
    """Raised internally when a caller field fails its bounds check."""

    def __init__(self, field: str, expectation: str) -> None:
        super().__init__(f"{field} must be {expectation}")
        self.field = field
        self.expectation = expectation


def _require_int(raw: Dict[str, Any], key: str, field: str, lo: int, hi: int) -> Optional[int]:
    """Parse an optional integer field. Absent -> None; present-but-bad -> reject."""
    if key not in raw or raw[key] is None:
        return None
    parsed = finite_int_in_range(raw[key], lo, hi)
    if parsed is None:
        raise _Rejected(field, f"a whole finite number between {lo:,} and {hi:,}")
    return parsed


def _require_float(raw: Dict[str, Any], key: str, field: str, lo: float, hi: float) -> Optional[float]:
    """Parse an optional decimal field. Absent -> None; present-but-bad -> reject."""
    if key not in raw or raw[key] is None:
        return None
    parsed = finite_in_range(raw[key], lo, hi)
    if parsed is None:
        raise _Rejected(field, f"a finite number between {lo:,g} and {hi:,g}")
    return parsed


def _validate_policies(portfolio: Any) -> List[Dict[str, Any]]:
    """Validate the policy list. Returns the accepted entries (possibly empty)."""
    if not isinstance(portfolio, dict):
        if portfolio in (None, {}):
            return []
        raise _Rejected("input_context.portfolio", "an object")

    raw_policies = portfolio.get("policies")
    if raw_policies is None:
        return []
    if not isinstance(raw_policies, list):
        raise _Rejected("input_context.portfolio.policies", "a list")
    if len(raw_policies) > _MAX_POLICIES:
        raise _Rejected("input_context.portfolio.policies", f"at most {_MAX_POLICIES} entries")

    accepted: List[Dict[str, Any]] = []
    for index, entry in enumerate(raw_policies):
        field = f"input_context.portfolio.policies[{index}]"
        if not isinstance(entry, dict):
            raise _Rejected(field, "an object")

        category = inert_identifier(entry.get("category"))
        if category is None:
            raise _Rejected(
                f"{field}.category",
                "a lowercase identifier starting with a letter (letters, digits and underscores, max 32)",
            )
        premium = _require_int(entry, "annual_premium_jpy", f"{field}.annual_premium_jpy", _PREMIUM_MIN, _PREMIUM_MAX)
        claims = _require_int(entry, "claims_count", f"{field}.claims_count", _CLAIMS_MIN, _CLAIMS_MAX)
        accepted.append(
            {
                "category": category,
                "annual_premium_jpy": 0 if premium is None else premium,
                "claims_count": 0 if claims is None else claims,
            }
        )
    return accepted


def _validate_overrides(raw: Any) -> Dict[str, Any]:
    """Validate per-request threshold overrides. Returns only accepted keys."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _Rejected("input_context.risk_policy_overrides", "an object")

    overrides: Dict[str, Any] = {}
    for key, (lo_i, hi_i) in _OVERRIDE_INT_BOUNDS.items():
        value = _require_int(raw, key, f"input_context.risk_policy_overrides.{key}", lo_i, hi_i)
        if value is not None:
            overrides[key] = value
    for key, (lo_f, hi_f) in _OVERRIDE_FLOAT_BOUNDS.items():
        value_f = _require_float(raw, key, f"input_context.risk_policy_overrides.{key}", lo_f, hi_f)
        if value_f is not None:
            overrides[key] = value_f
    return overrides


class AggregateCustomerProfileNode(FunctionNode):
    """Validate the caller contract and aggregate the policyholder portfolio.

    Input state keys:
        policy_id, validated_input, input_context, runtime_policy_json

    Output state keys (partial dict):
        policy_context_json: str
        status: str
        error_log: list[str]  (only on rejection)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        input_context: Dict[str, Any] = state.get("input_context", {}) or {}
        policy: Dict[str, Any] = from_json(state.get("runtime_policy_json"), None) or resolve_policy(None)

        try:
            customer_ref_raw = input_context.get("customer_ref")
            customer_ref = None
            if customer_ref_raw is not None:
                customer_ref = inert_identifier(customer_ref_raw)
                if customer_ref is None:
                    raise _Rejected(
                        "input_context.customer_ref",
                        "a lowercase identifier starting with a letter (letters, digits and underscores, max 32)",
                    )

            policies = _validate_policies(input_context.get("portfolio"))

            calendar = input_context.get("renewal_calendar") or {}
            if not isinstance(calendar, dict):
                raise _Rejected("input_context.renewal_calendar", "an object")
            days_to_renewal = _require_int(
                calendar, "days_to_renewal", "input_context.renewal_calendar.days_to_renewal", _DAYS_MIN, _DAYS_MAX
            )
            premium_change_pct = _require_float(
                calendar,
                "premium_change_pct",
                "input_context.renewal_calendar.premium_change_pct",
                _PCT_MIN,
                _PCT_MAX,
            )

            overrides = _validate_overrides(input_context.get("risk_policy_overrides"))
        except _Rejected as rejection:
            logger.error("AggregateCustomerProfileNode: rejected caller field %s", rejection.field)
            emit_trace_event(
                "customer_profile_rejected",
                {"field": rejection.field},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"AggregateCustomerProfileNode: {rejection}"],
            }

        # ── Aggregation ───────────────────────────────────────────────────────
        # Only totals leave this node in a renderable form. Per-policy premiums
        # stay inside the pipeline: the briefing publishes portfolio-level
        # figures, and a line item that is never carried cannot be rendered by
        # a later change to the formatter.
        covered_categories = sorted({entry["category"] for entry in policies})
        portfolio_annual_premium = sum(int(entry["annual_premium_jpy"]) for entry in policies)
        total_claims = sum(int(entry["claims_count"]) for entry in policies)

        thresholds = {
            "lapse_days_threshold": policy["lapse_days_threshold"],
            "premium_increase_pct_threshold": policy["premium_increase_pct_threshold"],
            "claims_count_threshold": policy["claims_count_threshold"],
        }
        thresholds.update(overrides)

        profile = {
            "policy_id": state.get("policy_id") or None,
            "customer_ref": customer_ref,
            "policy_count": len(policies),
            "covered_categories": covered_categories,
            "portfolio_annual_premium_jpy": portfolio_annual_premium,
            "total_claims_count": total_claims,
            "days_to_renewal": days_to_renewal,
            "premium_change_pct": premium_change_pct,
            "risk_thresholds": thresholds,
            "thresholds_overridden": sorted(overrides),
            "policy_version": policy["policy_version"],
            "policy_source": policy["source"],
            "policy_effective": policy["policy_effective"],
            "cross_sell_catalog": policy["cross_sell_catalog"],
            "caller_data_supplied": bool(policies) or days_to_renewal is not None,
        }

        emit_trace_event(
            "customer_profile_aggregated",
            {
                "policy_count": len(policies),
                "caller_data_supplied": profile["caller_data_supplied"],
                "thresholds_overridden": len(overrides),
            },
            state,
        )
        return {
            "policy_context_json": to_json(profile),
            "status": AgentStatus.SUCCESS.value,
        }
