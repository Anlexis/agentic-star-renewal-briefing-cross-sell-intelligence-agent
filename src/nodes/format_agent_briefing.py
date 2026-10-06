"""AgentCore Platform v1.0"""

# FormatAgentBriefingNode — inner domain node 5: compose the briefing.
#
# PUBLISHED FIGURE SET (the document's own contract, stated in the briefing and
# independently enforced at the output boundary):
#
#   * Monetary figures are PORTFOLIO-LEVEL AGGREGATES only — the annual
#     premium of the whole book and the indicative annual premium of the
#     recommended covers taken together. A single policy's premium is never
#     rendered, so a briefing left on a desk does not disclose one contract's
#     pricing.
#   * Every rendered monetary figure sits on a 1,000 grid. The rounding is
#     applied here, at render time, and re-checked at the boundary — the
#     boundary is the enforcement, this node is the intent.
#
# Rounding an aggregate does not contradict anything else the document states:
# the risk grade is derived from the renewal window, the premium movement and
# the claims count, never from the premium total, so the total can be rounded
# without the figure disagreeing with the verdict printed beside it.
#
# Every caller-supplied string reaching this document is an inert identifier
# validated upstream. The prose around them is the template's own.
#
# Inner node — ANONYMOUS trust.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import finite_int_in_range, from_json

logger = logging.getLogger(__name__)

# The published rounding grid. Must match the grid the output boundary
# enforces (src/nodes/security_gate_output.py) — the document RENDERS on this
# grid, the boundary ENFORCES it.
_RENDER_ROUND_UNIT = 1_000

_AGGREGATE_MIN = 0
_AGGREGATE_MAX = 100_000_000_000
_MAX_RENDERED_RECOMMENDATIONS = 5


def _render_amount(value: Any) -> str:
    """Render a monetary aggregate on the published grid.

    An unusable figure renders as 未確定 rather than as a number: a placeholder
    the reader can see is missing beats a zero they would read as a fact.
    """
    parsed = finite_int_in_range(value, _AGGREGATE_MIN, _AGGREGATE_MAX)
    if parsed is None:
        logger.warning("FormatAgentBriefingNode: aggregate outside its accepted range; rendered as undetermined")
        return "未確定"
    snapped = round(parsed / _RENDER_ROUND_UNIT) * _RENDER_ROUND_UNIT
    return f"{snapped:,d}円"


class FormatAgentBriefingNode(FunctionNode):
    """Compose the agent-ready renewal briefing.

    Input state keys:
        policy_context_json, renewal_risk_json, coverage_gaps_json, cross_sell_json

    Output state keys (partial dict):
        agent_briefing: str
        status: str
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        profile: Dict[str, Any] = from_json(state.get("policy_context_json"), {}) or {}
        risk: Dict[str, Any] = from_json(state.get("renewal_risk_json"), {}) or {}
        gaps: Dict[str, Any] = from_json(state.get("coverage_gaps_json"), {}) or {}
        cross_sell: Dict[str, Any] = from_json(state.get("cross_sell_json"), {}) or {}

        policy_id: Optional[str] = profile.get("policy_id")
        customer_ref: Optional[str] = profile.get("customer_ref")
        risk_level = str(risk.get("risk_level") or "未評価")
        risk_factors: List[str] = [str(f) for f in (risk.get("risk_factors") or [])]
        recommendations: List[Dict[str, Any]] = [
            r for r in (cross_sell.get("recommendations") or []) if isinstance(r, dict)
        ]

        header = customer_ref or policy_id or "対象契約"
        lines: List[str] = [
            f"# 更新ブリーフィング — {header}",
            "",
            f"- 契約ID: {policy_id or '未指定'}",
            f"- 対象契約数: {profile.get('policy_count', 0)}件",
            "",
            "## リスク評価",
            f"- 更新リスクレベル: **{risk_level}**",
        ]
        if risk_factors:
            for factor in risk_factors:
                lines.append(f"  - {factor}")
        else:
            lines.append("  - 該当するリスク要因はありません")

        lines += [
            "",
            "## ポートフォリオ規模",
            f"- 年間保険料合計: {_render_amount(profile.get('portfolio_annual_premium_jpy'))}",
            f"- クロスセル想定年間保険料: {_render_amount(cross_sell.get('opportunity_annual_premium_jpy'))}",
            "",
            "## カバレッジギャップ",
            f"- 未加入カテゴリ数: {gaps.get('gap_count', 0)}件",
        ]
        for gap_item in gaps.get("gaps_identified") or []:
            if not isinstance(gap_item, dict):
                continue
            label = str(gap_item.get("label") or gap_item.get("category") or "")
            lines.append(f"  - {label}: 未加入")

        lines += ["", "## クロスセル提案（優先順位順）"]
        if recommendations:
            for rec in recommendations[:_MAX_RENDERED_RECOMMENDATIONS]:
                label = str(rec.get("product_label") or rec.get("category") or "")
                talking_point = str(rec.get("talking_point") or "")
                lines.append(f"- 【{label}】 {talking_point}")
        else:
            lines.append("- 現時点でご提案できる未加入カテゴリはありません")

        applied = risk.get("applied_thresholds") or {}
        lines += [
            "",
            "## 適用ポリシー",
            f"- ポリシー版数: {profile.get('policy_version', '未指定')}（出所: {profile.get('policy_source', '未指定')}）",
            f"- 更新期日しきい値: {applied.get('lapse_days_threshold', '未指定')}日",
            f"- 保険料変動しきい値: {applied.get('premium_increase_pct_threshold', '未指定')}%",
            f"- 請求件数しきい値: {applied.get('claims_count_threshold', '未指定')}件",
        ]
        if profile.get("thresholds_overridden"):
            overridden = "、".join(str(k) for k in profile["thresholds_overridden"])
            lines.append(f"- リクエストによる上書き: {overridden}")
        if not profile.get("policy_effective", False):
            lines.append("- 注記: 運用設定が完全には適用されていないため、テンプレート既定値を含みます")

        lines += [
            "",
            "---",
            "_金額はポートフォリオ合計のみを 1,000 円単位に丸めて記載しています。個別契約の保険料は記載しません。_",
            "_本ブリーフィングは営業準備用の参考資料であり、保険契約の申込・引受・見積を構成するものではありません。_",
        ]

        briefing = "\n".join(lines)

        emit_trace_event(
            "agent_briefing_formatted",
            {
                "briefing_chars": len(briefing),
                "recommendation_count": len(recommendations),
                "risk_level": risk_level,
            },
            state,
        )
        return {
            "agent_briefing": briefing,
            "status": AgentStatus.SUCCESS.value,
        }
