"""The output boundary: screen, redact, enforce the published grid, screen again.

The briefing states a figure contract — monetary values are portfolio
aggregates on a 1,000 grid, per-policy premiums are never rendered. This file
is the proof that the contract is ENFORCED at release rather than merely
observed by the formatter, and that enforcement leaves the document's
identifiers and structural numbers untouched.
"""

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import RenewalBriefingCrossSellAgent
from src.nodes.security_gate_output import (
    WITHHELD_NOTICE,
    SecurityGateOutputNode,
    UnprocessableNumericToken,
    _enforce_precision,
    _redact_blocked_fields,
    _security_gate_output,
)


class TestGridIsEnforcedForEveryRepresentation:
    """Every form a monetary figure can take, not only the convenient ones."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            # comma-grouped, at any magnitude — no exemption for small amounts
            ("156,499円", "156,000円"),
            ("9,999 JPY", "10,000 JPY"),
            ("1,234,567", "1,235,000"),
            # unformatted runs of five digits or more
            ("123456", "123,000"),
            # short values in currency context, marker first
            ("JPY 9999", "JPY 10,000"),
            ("¥9999", "¥10,000"),
            # symmetric: marker after the value, code or symbol
            ("9999 JPY", "10,000 JPY"),
            ("9999円", "10,000円"),
            ("9999₩", "10,000₩"),
            ("9999￥", "10,000￥"),
            # signed, sign preserved
            ("JPY-9999", "JPY-10,000"),
            ("JPY +9999", "JPY +10,000"),
            # arbitrary horizontal whitespace, and a single newline
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
            ("JPY\n9999", "JPY\n10,000"),
            # decimal amounts round as ONE number: the whole value goes on the
            # grid and the fraction is not rewritten in place
            ("JPY 1234.56", "JPY 1,000"),
            ("1234.56 JPY", "1,000 JPY"),
            ("\u00a51234.56", "\u00a51,000"),
            ("156,499.75\u5186", "156,000\u5186"),
        ],
    )
    def test_off_grid_value_snaps(self, text, expected):
        result, snaps = _enforce_precision(text)
        assert result == expected
        assert snaps == 1

    def test_grouped_value_snaps_as_a_whole_token(self):
        """Leftmost-first matching must not consume "JPY 1" out of "JPY 1,234".

        The comma-grouped alternative comes first in the marker-then-value
        branch precisely so this cannot happen; without it the snap corrupts
        the number instead of rounding it.
        """
        assert _enforce_precision("JPY 1,000") == ("JPY 1,000", 0)
        assert _enforce_precision("JPY 1,234") == ("JPY 1,000", 1)


class TestStructuralTokensSurvive:
    """The boundary must not rewrite the things the briefing exists to name."""

    @pytest.mark.parametrize(
        "text",
        [
            "POL-880123",
            "INS-ABCD1234",
            "cust_880123",
            "plan_48210",
            "更新期日まで 21 日",
            "保険料変動 12.5%",
            "当期の請求件数 3 件",
            "in 2026",
            "v12",
            "STAR 2026",
            "156,000円",
            "1,000 円単位",
        ],
    )
    def test_identifiers_and_structural_numbers_are_byte_identical(self, text):
        assert _enforce_precision(text) == (text, 0)

    def test_a_section_heading_after_a_three_letter_code_is_untouched(self):
        """A paragraph-spanning delimiter would let the boundary renumber sections.

        With a `\\s*` delimiter the three-letter code at the end of one line
        binds to the number that opens the next block, and "3. Cash Position"
        is released as "0. Cash Position" — the boundary rewriting document
        structure rather than rounding a figure.
        """
        text = "Currency: JPY\n\n3. Cash Position"
        assert _enforce_precision(text) == (text, 0)

    def test_a_pure_digit_identifier_cannot_be_rendered(self):
        """The complement of the guard, closed on the input side.

        Guards protect a digit run that sits beside a letter, digit, hyphen or
        underscore. A purely numeric identifier has nothing beside it, so the
        inert identifier form requires a leading letter and such a code is
        refused before it can reach the document.
        """
        from src.schemas.state import inert_identifier

        assert inert_identifier("48210") is None
        assert _enforce_precision("48210") == ("48,000", 1)


class TestDecimalsAreNotRewritten:
    """A decimal's FRACTION is never treated as a number of its own.

    The fraction of "9999.99999" is a standalone five-digit run, and the
    identifier guards admit it because a decimal point is not an identifier
    character. A grammar that does not absorb the decimal into the value snaps
    the fraction in place and releases "9999.100,000" — neither the true value
    nor a grid value, and silently. The fix is to match the whole number and
    to put the decimal point in the LEADING guard only; putting it in the
    trailing guard as well would let an amount that ends a sentence escape the
    grid entirely.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            "0.123456",
            # figures this briefing actually renders
            "\u4fdd\u967a\u6599\u5909\u52d5 12.5%",
            "\u30dd\u30ea\u30b7\u30fc\u7248\u6570: 1.0.0",
            "2026-07-02",
        ],
    )
    def test_a_fraction_is_never_snapped_in_place(self, text):
        assert _enforce_precision(text) == (text, 0)

    def test_an_on_grid_decimal_amount_stays_byte_identical(self):
        assert _enforce_precision("JPY 1,000.00") == ("JPY 1,000.00", 0)

    def test_an_off_grid_decimal_amount_rounds_as_one_number(self):
        """The value rounds; the fraction is not left dangling beside it."""
        result, snaps = _enforce_precision("JPY 1234.56")
        assert result == "JPY 1,000"
        assert snaps == 1
        assert "1,000.56" not in result

    def test_a_suffixed_decimal_cannot_backtrack_into_a_dangling_fraction(self):
        r"""A bare-optional fraction reintroduces the dangling-fraction bug.

        With `(?:\.\d+)?` the engine backtracks out of ".56" the moment the
        trailing guard rejects the "m" after it, and re-matches "1234" on its
        own — "JPY 1234.56m" released as "JPY 1,000.56m". The two-arm
        absorption takes the fraction whole or proves there is none.
        """
        assert _enforce_precision("JPY 1234.56m") == ("JPY 1234.56m", 0)

    def test_the_decimal_point_is_not_in_the_trailing_guard(self):
        """An amount ending a sentence must still be caught.

        If the decimal point joined the trailing guard too, a match could not
        end immediately before one — and an off-grid amount at the end of a
        sentence would escape the grid.
        """
        result, snaps = _enforce_precision("The book totals JPY 9999.")
        assert result == "The book totals JPY 10,000."
        assert snaps == 1


class TestLayerOrder:
    """The pattern scan reads the intact document, and reads it again after."""

    def test_a_credential_is_caught_on_the_intact_document(self):
        assert _security_gate_output("briefing sk-abcdefghij1234567890") == "api_key_pattern"

    def test_the_snap_does_not_destroy_a_pattern_before_it_is_scanned(self):
        """A digit run inside a credential must not be rewritten first.

        Verbatim field redaction is order-independent; a pattern scan is not.
        Rewriting first can leave the secret in the document in a shape the
        scan no longer recognises. Both the guard (the run sits beside letters)
        and the ordering keep that from happening.
        """
        secret = "sk-abcdefghij1234567890"
        snapped, _ = _enforce_precision(secret)
        assert snapped == secret
        assert _security_gate_output(snapped) == "api_key_pattern"

    def test_a_hyphenated_personal_number_survives_the_snap_intact(self):
        """A structured identifier keeps the shape a pattern scan matches on.

        An unguarded grammar reads "SSN" as a currency marker and rewrites the
        first group, destroying the very pattern a later screen would catch.
        """
        text = "SSN 123-45-6789"
        assert _enforce_precision(text) == (text, 0)

    @pytest.mark.parametrize(
        "payload,violation",
        [
            ("sk-abcdefghij1234567890", "api_key_pattern"),
            ("AKIA1234567890ABCDEF", "access_key_pattern"),
            ("eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM", "jwt_pattern"),
            ("Bearer abcdefgh12345678", "bearer_token"),
            ("password: hunter2hunter2", "credential_assignment"),
            ("<script>alert(1)</script>", "script_fragment"),
        ],
    )
    def test_blocked_patterns_are_named(self, payload, violation):
        assert _security_gate_output(f"# 更新ブリーフィング\n{payload}") == violation

    def test_clean_briefing_passes(self):
        assert _security_gate_output("# 更新ブリーフィング — cust_1\n- 年間保険料合計: 156,000円") is None


class TestVerbatimCallerTextRedaction:
    def test_caller_query_embedded_verbatim_is_redacted(self):
        state = {"validated_input": "更新ブリーフィングを作成してください"}
        text = "# 更新ブリーフィング\n更新ブリーフィングを作成してください"
        sanitised, fields = _redact_blocked_fields(text, state)
        assert "[REDACTED]" in sanitised
        assert fields == ["validated_input"]

    def test_short_incidental_overlap_is_not_redacted(self):
        state = {"validated_input": "更新"}
        text = "# 更新ブリーフィング"
        sanitised, fields = _redact_blocked_fields(text, state)
        assert sanitised == text
        assert fields == []

    def test_caller_text_nested_inside_a_blocked_field_is_redacted(self):
        """A blocked field is not always a bare string.

        `enriched_context` is a mapping, so caller text can ride a level or two
        down inside it. A layer that inspects only top-level strings walks past
        that text and the embedding ships. The top-level case above is the
        control that proves this probe is testing the walk, not the matcher.
        """
        state = {"enriched_context": {"request": {"note": "更新ブリーフィングを作成してください"}}}
        text = "# 更新ブリーフィング\n更新ブリーフィングを作成してください"
        sanitised, fields = _redact_blocked_fields(text, state)
        assert "[REDACTED]" in sanitised
        assert fields == ["enriched_context.request.note"]

    def test_the_nested_walk_is_depth_bounded(self):
        deep: dict = {}
        current = deep
        for _ in range(12):
            current["next"] = {}
            current = current["next"]
        current["note"] = "更新ブリーフィングを作成してください"
        state = {"enriched_context": deep}
        text = "更新ブリーフィングを作成してください"
        sanitised, fields = _redact_blocked_fields(text, state)
        assert sanitised == text
        assert fields == []


class TestOutputNodeBehaviour:
    """What the node returns, at the level a caller observes."""

    def test_clean_briefing_is_released(self):
        briefing = "# 更新ブリーフィング — cust_1\n- 年間保険料合計: 156,000円"
        result = SecurityGateOutputNode().execute({"result": briefing})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == briefing

    def test_off_grid_figure_is_snapped_on_release(self):
        result = SecurityGateOutputNode().execute({"result": "- 年間保険料合計: 156,499円"})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "156,000円" in result["formatted_output"]
        assert "156,499" not in result["formatted_output"]

    def test_blocked_briefing_is_withheld_entirely(self):
        blocked = "briefing sk-abcdefghij1234567890"
        result = SecurityGateOutputNode().execute({"result": blocked})
        assert result["status"] == AgentStatus.ERROR.value
        # Withheld means REPLACED, not merely unwritten: an absent key leaves
        # the rejected document in the state the response is built from.
        assert result["formatted_output"] == WITHHELD_NOTICE
        assert blocked not in repr(result)
        assert "api_key_pattern" in result["error_log"][0]

    def test_oversize_briefing_is_withheld(self):
        result = SecurityGateOutputNode().execute({"result": "あ" * 16_001})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == WITHHELD_NOTICE

    def test_an_unroundable_numeric_token_withholds_the_briefing(self):
        """A figure the grid cannot be applied to is withheld, not passed through.

        Python refuses to convert absurdly long digit strings to and from int,
        so a pathological run cannot be rounded. Releasing it unrounded would
        put an unenforced figure on the external surface — exactly what the
        grid exists to prevent.
        """
        pathological = "1" * 5000
        with pytest.raises(UnprocessableNumericToken):
            _enforce_precision(pathological)

        result = SecurityGateOutputNode().execute({"result": f"- 年間保険料合計: {pathological}円"})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == WITHHELD_NOTICE
        assert pathological not in repr(result)
        assert "unprocessable_numeric_token" in result["error_log"][0]

    def test_withholding_never_echoes_the_content(self):
        secret = "sk-abcdefghij1234567890"
        result = SecurityGateOutputNode().execute({"result": f"briefing {secret}"})
        assert secret not in result["error_log"][0]


_BRIEFING_WITH_A_CREDENTIAL = (
    "# 更新ブリーフィング — cust_880123\n"
    "- 契約ID: POL-880123\n"
    "- 年間保険料合計: 156,000円\n"
    "API: sk-abcdefghij1234567890"
)
_SECRET = "sk-abcdefghij1234567890"


class TestWithholdingClearsTheDocument:
    """A refusal that leaves the document in state is not a refusal.

    A node returns a PARTIAL state update: fields it omits keep their previous
    values. `result` holds the briefing exactly as the pipeline produced it, so
    a withholding update that carries only a status leaves the rejected
    document sitting in the state the response is built from.
    """

    def _delta(self, briefing=_BRIEFING_WITH_A_CREDENTIAL):
        return SecurityGateOutputNode().execute({"result": briefing})

    @pytest.mark.parametrize("field", ["formatted_output", "result", "agent_briefing"])
    def test_every_field_that_could_carry_the_briefing_is_replaced(self, field):
        delta = self._delta()
        assert delta["status"] == AgentStatus.ERROR.value
        assert field in delta, f"{field} still holds whatever it held before the refusal"
        assert delta[field] == WITHHELD_NOTICE

    @pytest.mark.parametrize("field", ["formatted_output", "result", "agent_briefing"])
    def test_the_replacement_is_truthy(self, field):
        """An empty replacement is not a cleared field.

        The response envelope resolves the caller-facing document as
        `formatted_output or result`. "" and None are falsy, so they hand the
        resolution straight back to `result` — the anti-pattern that keeps the
        withheld briefing shipping.
        """
        assert bool(self._delta()[field]) is True

    def test_the_withheld_document_is_nowhere_in_the_update(self):
        delta = self._delta()
        assert _SECRET not in repr(delta)
        assert "156,000円" not in repr(delta)

    def test_every_violation_class_withholds_the_same_way(self):
        """Not only the credential path — every branch that refuses to release."""
        oversize = SecurityGateOutputNode().execute({"result": "あ" * 16_001})
        unroundable = SecurityGateOutputNode().execute({"result": f"- 合計: {'1' * 5000}円"})
        for delta in (oversize, unroundable):
            assert delta["status"] == AgentStatus.ERROR.value
            assert delta["formatted_output"] == WITHHELD_NOTICE
            assert delta["result"] == WITHHELD_NOTICE
            assert delta["agent_briefing"] == WITHHELD_NOTICE

    def test_the_clearing_survives_the_frameworks_own_output_scan(self):
        """Through the real node wrapper, not just execute().

        The framework scans every value of the update for credential patterns
        and raises on a hit; the wrapper turns that into its own bare error
        update, which clears nothing. So a refusal message that quoted the
        matched text would discard this clearing and release the document. The
        message names the violation class only — this is the proof that the
        whole update reaches the caller intact.
        """
        delta = SecurityGateOutputNode()(
            {
                "result": _BRIEFING_WITH_A_CREDENTIAL,
                "status": AgentStatus.SUCCESS.value,
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            }
        )
        assert delta["status"] == AgentStatus.ERROR.value
        assert delta["formatted_output"] == WITHHELD_NOTICE
        assert delta["result"] == WITHHELD_NOTICE
        assert _SECRET not in repr(delta)
        assert "Traceback" not in repr(delta)


class TestTheEnvelopeReleasesOnlyOnSuccess:
    """The accessor that builds the caller's response is a boundary too.

    It resolves the document as `formatted_output or result` — `result` being
    the briefing as the pipeline produced it, before the boundary screened it.
    Without a status check the accessor re-publishes exactly what the boundary
    refused, so the refusal upstream never gets the chance to matter.
    """

    def _agent(self):
        return RenewalBriefingCrossSellAgent()

    @pytest.mark.parametrize(
        "status",
        [
            AgentStatus.ERROR.value,
            AgentStatus.TIMEOUT.value,
            AgentStatus.CANCELLED.value,
            AgentStatus.PENDING.value,
        ],
    )
    def test_a_failed_run_publishes_no_document_however_full_the_state(self, status):
        envelope = self._agent().get_output(
            {
                "status": status,
                "result": _BRIEFING_WITH_A_CREDENTIAL,
                "formatted_output": _BRIEFING_WITH_A_CREDENTIAL,
                "trace_id": "t",
                "correlation_id": "c",
            }
        )
        assert envelope["status"] == status
        assert not envelope["output"]
        assert _SECRET not in repr(envelope["output"])
        assert "更新ブリーフィング" not in str(envelope["output"])

    def test_the_fallback_to_result_is_gone_on_a_failed_run(self):
        """The shape a withholding leaves behind when it sets only a status."""
        envelope = self._agent().get_output({"status": AgentStatus.ERROR.value, "result": _BRIEFING_WITH_A_CREDENTIAL})
        assert not envelope["output"]

    def test_a_successful_run_still_publishes_the_released_document(self):
        """The control: containment must not cost the agent its output."""
        released = "# 更新ブリーフィング — cust_880123\n- 年間保険料合計: 156,000円"
        envelope = self._agent().get_output(
            {
                "status": AgentStatus.SUCCESS.value,
                "formatted_output": released,
                "trace_id": "t",
                "correlation_id": "c",
            }
        )
        assert envelope["output"] == released
        assert envelope["status"] == AgentStatus.SUCCESS.value
