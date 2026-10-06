"""AgentCore Platform v1.0"""

# SecurityGateOutputNode — the outer post_process slot: the output boundary.
#
# Four layers, in this order, on the briefing about to be released:
#
#   (1) PATTERN SCAN — credentials, tokens and script fragments anywhere in the
#       document withhold it entirely.
#   (2) VERBATIM CALLER-TEXT REDACTION — the briefing is composed from
#       validated portfolio data and the template's own prose, so a verbatim
#       embedding of the caller's query text is caller-controlled content on an
#       external surface, and is replaced.
#   (3) PRECISION GRID — the briefing states that monetary figures are
#       portfolio aggregates on a 1,000 grid. This layer ENFORCES that
#       statement independently of the formatter that made it: any off-grid
#       monetary token is snapped, with an audit event.
#   (4) RE-SCAN — layer 1 again.
#
# WHY THE SCAN RUNS BEFORE THE SNAP, AND AGAIN AFTER. The snap rewrites digit
# runs. A credential such as an API key ends in a long run of characters that
# can include digits, so snapping first can break the very shape the scan
# matches on: the secret would still be in the document, no longer recognisable,
# and it would ship. Verbatim field redaction is order-independent; a pattern
# scan is not. Scanning first catches the intact form, and re-scanning after
# catches anything the snap could have assembled into a match.
#
# The gate logic lives in module-level functions, not instance methods on the
# node class: the framework auto-wraps node instance methods named like gates
# on the real invoke path, which would raise at runtime.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

logger = logging.getLogger(__name__)

# ── Withholding: what the caller gets instead of the briefing ─────────────────

# The notice that REPLACES the document in state when the boundary withholds it.
#
# It has to be non-empty. The response envelope resolves the caller-facing
# document as `formatted_output or result`, with no reference to the run's
# status, so an empty string or None is not a cleared field at all: it is falsy,
# it re-activates the fallback, and the withheld briefing ships anyway inside
# the error envelope. A truthy placeholder occupies the field and the fallback
# never fires.
#
# It is a fixed, inert string. Naming the violated rule is the job of the audit
# event and the error log; anything derived from the document would put the
# withheld content back on the surface the withholding exists to protect.
WITHHELD_NOTICE = "[保留] ブリーフィングは出力境界で保留されました。"

# Every state field that can carry the briefing, cleared as one unit.
#
# Returning an error status is not containment on its own. A node returns a
# PARTIAL state update, so any field the update omits keeps the value it already
# had: `result` still holds the document the scan just rejected, and the
# envelope reads it. `agent_briefing` is the boundary's own fallback input
# (execute() reads `result or agent_briefing`), so leaving it set would hand the
# document to a re-read of the same state. All three are replaced together.
_CLEARED_ON_WITHHOLD: Dict[str, Any] = {
    "formatted_output": WITHHELD_NOTICE,
    "result": WITHHELD_NOTICE,
    "agent_briefing": WITHHELD_NOTICE,
}

# ── Layer 1/4: patterns that must never appear in a released briefing ─────────

_BLOCKED_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}"), "api_key_pattern"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "access_key_pattern"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "jwt_pattern"),
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{8,}"), "bearer_token"),
    (
        re.compile(r"(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}", re.I),
        "credential_assignment",
    ),
    (re.compile(r"<\s*script[\s>/]", re.I), "script_fragment"),
]

_MAX_OUTPUT_LEN = 16_000

# ── Layer 2: state fields that must never be embedded verbatim ───────────────

_BLOCKED_FIELDS = frozenset({"user_input", "validated_input", "enriched_context"})
_MIN_VERBATIM_LEN = 10
_MAX_FIELD_DEPTH = 6

# ── Layer 3: the published precision grid ────────────────────────────────────

# Must match the grid the briefing renders on (src/nodes/format_agent_briefing.py).
_EXTERNAL_ROUND_UNIT = 1_000

# Identifiers this briefing renders — policy identifiers (INS-XXXXXX,
# POL-123456) and the inert caller identifiers (cust_8842, plan_a1) — are made
# of letters, digits, hyphens and underscores. The monetary grammar below
# treats any standalone three-letter uppercase word as a currency marker and
# any long digit run as an amount, so without a guard "POL-123456" would render
# as "POL-123,000" and "plan_48210" as "plan_48,000": the boundary silently
# renaming the things the briefing exists to name. The guards are
# single-character assertions on both ends of the match — a monetary token may
# not sit directly beside a letter, digit, hyphen or underscore. Underscore is
# in the class because the caller-identifier alphabet uses it; read your own
# render alphabet before reusing this. Being fixed-width, the guards do not
# constrain the variable-width delimiter inside the match.
#
# A purely numeric identifier would have no adjacent character to protect it,
# which is why the inert identifier form requires a leading letter
# (src/schemas/state.py) — the guard and the input rule close the same hole
# from two sides.
_IDENTIFIER_CHAR = r"[A-Za-z0-9_-]"

# The LEADING guard carries one extra character: the decimal point. A monetary
# token may begin a number but never begins inside a FRACTION, and the trailing
# guard cannot carry it — an amount that ends a sentence would then escape the
# grid entirely.
_LEADING_GUARD_CHAR = r"[A-Za-z0-9_.-]"

_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"

# Delimiter between a currency marker and its value: horizontal whitespace and
# at most ONE newline — never a paragraph break. A plain `\s*` spans blank
# lines, so a three-letter uppercase word ending a line binds to the number
# that opens the next block and the gate rewrites document structure
# ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position"). Every leak form
# (spaces, tabs, a single newline, signed, symmetric, comma-grouped) still
# matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A monetary amount may carry a decimal part, and each value alternative
# absorbs it as part of the same token. Without that, the fraction of
# "9999.99999" is a standalone five-digit run all on its own: the guards admit
# it, because a decimal point is not an identifier character, and the snap
# rewrites the fraction rather than the number — "8.512345" becomes
# "8.512,000" and "JPY 1234.56" becomes "JPY 1,000.56", which is neither the
# true value nor a grid value. Matching the whole number instead means
# "JPY 1234.56" snaps to "JPY 1,000" and a ratio or percentage, having no
# currency context and too few leading digits, is left alone.
# The absorption must not be a bare optional: `(?:\.\d+)?` allows the engine
# to give the fraction back and re-match the integer part alone when whatever
# follows the fraction fails the trailing guard, so "JPY 1234.56m" came back
# "JPY 1,000.56m". Hence the two-arm form — take the fraction whole, or
# assert there is no fraction to take.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

# Group-based grammar — no variable-width lookbehinds, so the marker/value
# delimiter can be an arbitrary run of horizontal whitespace. Every value
# accepts an optional explicit +/- sign. Branch order matters: currency-context
# branches first, then the form-based branch. Monetary values are identified by
# FORM and by CURRENCY CONTEXT, never by magnitude — an exemption for small
# amounts is an exemption for the exact figures that are easiest to attribute
# to one policyholder.
_NUM_TOKEN_RE = re.compile(
    rf"(?<!{_LEADING_GUARD_CHAR})"
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives take the comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1" and
    # the snap would mangle the number — an on-grid "JPY 1,000" must stay
    # byte-identical, and an off-grid "JPY 1,234" must snap as 1234, not as 1.
    rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})"
    rf"(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))"
    rf"(?!{_IDENTIFIER_CHAR})"
)


def _security_gate_output(content: str) -> Optional[str]:
    """Scan a document for disallowed patterns.

    Returns the name of the first violation, or None when the document is
    clean. Module-level function, not a node instance method.
    """
    if not content:
        return None
    if len(content) > _MAX_OUTPUT_LEN:
        return "oversize_output"
    for pattern, name in _BLOCKED_PATTERNS:
        if pattern.search(content):
            return name
    return None


def _string_leaves(value: Any, path: str, depth: int = 0) -> List[Tuple[str, str]]:
    """Collect every string inside *value*, with the path it was found at.

    A blocked field is not always a bare string: `enriched_context` is a
    mapping, and caller text can ride one or more levels down inside it. A
    redaction layer that inspects only top-level strings walks straight past
    that text and the embedding ships. The walk is depth-bounded, because the
    structure being walked is shaped by a caller.
    """
    if depth > _MAX_FIELD_DEPTH:
        return []
    if isinstance(value, str):
        return [(path, value)]
    leaves: List[Tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            leaves.extend(_string_leaves(child, f"{path}.{str(key)[:32]}", depth + 1))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            leaves.extend(_string_leaves(child, f"{path}[{index}]", depth + 1))
    return leaves


def _redact_blocked_fields(result: str, state: AgentState) -> Tuple[str, List[str]]:
    """Replace verbatim embeddings of caller-derived state text with [REDACTED].

    Returns (sanitised_result, redacted_field_paths). Nested values are walked,
    not just top-level strings. Only substantial values are matched, so a short
    incidental overlap between a query and the template's own prose is not
    redacted.
    """
    redacted: List[str] = []
    sanitised = result
    for field in sorted(_BLOCKED_FIELDS):
        for path, value in _string_leaves(state.get(field), field):
            if len(value) > _MIN_VERBATIM_LEN and value in sanitised:
                sanitised = sanitised.replace(value, "[REDACTED]")
                redacted.append(path)
    return sanitised, redacted


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class UnprocessableNumericToken(Exception):
    """A matched numeric token could not be put on the grid.

    Python refuses to convert absurdly long digit strings to and from int, so a
    pathological run inside a document would raise here. The boundary treats
    that as a reason to WITHHOLD the document rather than to pass the token
    through unrounded: a figure the grid could not be applied to is exactly the
    figure the grid exists to catch.
    """


def _enforce_precision(result: str) -> Tuple[str, int]:
    """Snap every monetary-form token onto the published grid.

    Returns (sanitised_result, snap_count). A snap means a full-precision
    monetary figure reached the external surface and the boundary rounded it.
    The currency marker, the original delimiter whitespace and the explicit
    sign of the original token are all preserved on the replacement.

    Raises UnprocessableNumericToken when a matched token cannot be converted.
    """
    snaps = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal snaps
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        try:
            value = float(token.replace(",", ""))  # float() understands a leading +/-
            if value % _EXTERNAL_ROUND_UNIT == 0:
                return match.group(0)
            snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
            plus = "+" if token.startswith("+") and snapped >= 0 else ""
            replacement = f"{pre}{plus}{snapped:,d}{post}"
        except (ValueError, OverflowError) as exc:
            raise UnprocessableNumericToken(f"numeric token of {len(token)} characters") from exc
        snaps += 1
        return replacement

    return _NUM_TOKEN_RE.sub(_snap, result), snaps


class SecurityGateOutputNode(FunctionNode):
    """The output boundary: screen, redact, enforce the grid, screen again.

    Reads `result` (written by RenewalBriefingGraphNode.merge_output) and
    writes `formatted_output`. A blocked document is withheld entirely — the
    node returns an error status AND replaces every field that could still be
    carrying the document with a withheld notice, so nothing partially
    sanitised, and nothing the scan rejected, is left for a reader to pick up.

    Declared ANONYMOUS: caller trust was already enforced at the pre_process
    slot, which requires VERIFIED_EXTERNAL.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "result": message,
                "formatted_output": message,
            }
        output_text: str = state.get("result") or state.get("agent_briefing") or ""

        # ── Layer 1: pattern scan on the intact document ──────────────────────
        violation = _security_gate_output(output_text)
        if violation:
            return self._withhold(state, violation, layer="pre_snap")

        # ── Layer 2: verbatim caller-text redaction ───────────────────────────
        sanitised, redacted_fields = _redact_blocked_fields(output_text, state)
        if redacted_fields:
            logger.error(
                "SecurityGateOutputNode: caller-derived text embedded verbatim in the briefing — %s",
                ", ".join(redacted_fields),
            )
            emit_trace_event("output_blocked_field_redaction", {"fields": redacted_fields}, state)

        # ── Layer 3: precision grid ───────────────────────────────────────────
        try:
            sanitised, snaps = _enforce_precision(sanitised)
        except UnprocessableNumericToken:
            return self._withhold(state, "unprocessable_numeric_token", layer="grid")
        if snaps:
            logger.warning("SecurityGateOutputNode: %d off-grid monetary token(s) snapped", snaps)
            emit_trace_event("output_precision_snap", {"snap_count": snaps}, state)

        # ── Layer 4: re-scan the document the snap produced ───────────────────
        violation = _security_gate_output(sanitised)
        if violation:
            return self._withhold(state, violation, layer="post_snap")

        emit_trace_event("output_gate_passed", {"output_chars": len(sanitised)}, state)
        return {
            "formatted_output": sanitised,
            "status": AgentStatus.SUCCESS.value,
        }

    def _withhold(self, state: AgentState, violation: str, layer: str) -> Dict[str, Any]:
        """Withhold the briefing: error status AND every carrying field replaced.

        The returned mapping names the violation CLASS and the layer that
        raised it, and quotes nothing from the document. That is not only a
        logging convention: the framework runs its own credential scan over
        every value of this mapping, and a message quoting the matched text
        would trip it. The scan raises, the node wrapper converts the raised
        error into its own bare error update, and that update clears nothing —
        so echoing the finding would discard the very clearing performed here
        and release the document it was meant to withhold.
        """
        logger.error("SecurityGateOutputNode: briefing withheld — %s (%s)", violation, layer)
        emit_trace_event("output_gate_blocked", {"violation": violation, "layer": layer}, state)
        return {
            **_CLEARED_ON_WITHHOLD,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"SecurityGateOutputNode: output withheld — {violation}"],
        }
