# docs/02_design.md — INS-C2-018 RenewalBriefingCrossSellAgent

## Template Metadata

| Field | Value |
|---|---|
| Template ID | INS-C2-018 |
| Category | Cat 2 (multi-step domain workflow — document generation) |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Industry | INS (Insurance) |
| Pattern | Multi-source aggregation + briefing generation |
| Generation mode | Deterministic — no model is invoked |

## Architecture Overview

The agent uses the **nested graph pattern**: an outer `AgentBaseGraph` (fixed five-node backbone)
with a `GraphNode` subclass in the `main` slot wrapping an inner `BaseGraph` (the domain pipeline).

### Outer graph — `src/graph/graph.py`

`RenewalBriefingCrossSellAgent(AgentBaseGraph)` — fixed backbone:

```
START → initialize → ValidateInputNode (pre_process)
      → RenewalBriefingGraphNode (main)
      → SecurityGateOutputNode (post_process)
      → finalize → END
```

- `pre_process`: `ValidateInputNode` — the caller boundary (`VERIFIED_EXTERNAL`)
- `main`: `RenewalBriefingGraphNode` — `GraphNode` delegating to `DomainWorkflowGraph`
- `post_process`: `SecurityGateOutputNode` — the output boundary

### Inner graph — `src/graph/domain_workflow_graph.py`

`DomainWorkflowGraph(BaseGraph)` — linear five-node domain pipeline:

```
START → AggregateCustomerProfileNode
      → IdentifyRenewalRiskNode
      → DetectCoverageGapsNode
      → GenerateCrossSellRecommendationsNode
      → FormatAgentBriefingNode
      → END
```

### Crossing the boundary — `src/graph/context_bridge.py`

`GraphNode.execute()` invokes the inner graph with the input string only; it does not forward the
outer state. Two things the pipeline needs would therefore be lost at the boundary: the caller's
`input_context` (the portfolio) and the resolved `policy_id`. Both are handed across explicitly —
`extract_input()` stashes them in a `ContextVar`, and the inner graph's `_extra_initial_state()`
seeds them into the inner state. A `ContextVar` keeps the hand-off per-thread and per-task, so
concurrent invocations in one process cannot read each other's data.

Without the bridge every request returns the same empty-portfolio briefing while reporting
success, which is why the guarantee is asserted end to end
(`tests/proof_of_boundary/test_pb_invoke_endpoint.py`) rather than at node level.

## Node Responsibilities

| Node | Slot / Layer | File | Responsibility |
|---|---|---|---|
| `ValidateInputNode` | outer `pre_process` | `src/nodes/validate_input.py` | Screen the query and the context channel; resolve `policy_id` |
| `RenewalBriefingGraphNode` | outer `main` | `src/graph/graph.py` | `GraphNode`; delegates to `DomainWorkflowGraph`; forwards runtime config; skips the subgraph on a recorded rejection |
| `AggregateCustomerProfileNode` | inner | `src/nodes/aggregate_customer_profile.py` | Validate the caller-data contract; aggregate the portfolio |
| `IdentifyRenewalRiskNode` | inner | `src/nodes/identify_renewal_risk.py` | Grade renewal risk against the effective thresholds |
| `DetectCoverageGapsNode` | inner | `src/nodes/detect_coverage_gaps.py` | Compare held cover against the configured catalog |
| `GenerateCrossSellRecommendationsNode` | inner | `src/nodes/generate_cross_sell_recommendations.py` | Rank recommendations; size the opportunity |
| `FormatAgentBriefingNode` | inner | `src/nodes/format_agent_briefing.py` | Compose the briefing on the published figure set |
| `SecurityGateOutputNode` | outer `post_process` | `src/nodes/security_gate_output.py` | Screen, redact, enforce the grid, screen again — or publish the recorded rejection reason when there is no briefing to screen |

## Configuration

Two files, with different jobs, and the distinction is load-bearing:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | Registration manifest — flat, every key at root level. Identity, entry class, declared trust level, required secrets and extras. No tuning values. | The platform registry |
| `config/config.yaml` | Runtime parameters — renewal thresholds, cross-sell catalog, `max_retry`, `timeout_s`. | `src/services/renewal_policy.py`, the single loader |

`RenewalBriefingGraphNode._parent_config()` reads `config/config.yaml` and forwards it to the inner
graph under `config["configurable"]`, additionally exposing `timeout_s` as `timeout_seconds` — the
name the inner layer validates — so a declared value is not lost to a key-name mismatch. The agent
constructor defaults to the same file, so constructing it directly does not silently yield an empty
config.

`resolve_policy()` bounds every value it reads. A threshold that is absent, non-finite or out of
range is **not applied**: the documented default is kept, `policy_effective` is set False, and the
briefing prints a note saying the configuration was not fully applied. A configuration that is
declared, forwarded and then quietly ignored is the failure this design exists to make visible.

## The caller-data contract

`/invoke` accepts `input_context` alongside `input`. Every field is optional — an absent context
degrades to the empty-portfolio baseline — and every field present is validated against explicit
bounds. A present-but-invalid field is a **rejection**, never a substituted default.

| Field | Type | Bounds |
|---|---|---|
| `customer_ref` | inert identifier | `[a-z][a-z0-9_]{0,31}` |
| `portfolio.policies[]` | list | at most 50 entries |
| `portfolio.policies[].category` | inert identifier | `[a-z][a-z0-9_]{0,31}` |
| `portfolio.policies[].annual_premium_jpy` | integer | 0 … 100,000,000 |
| `portfolio.policies[].claims_count` | integer | 0 … 1,000 |
| `renewal_calendar.days_to_renewal` | integer | 0 … 3,650 |
| `renewal_calendar.premium_change_pct` | number | −100 … 1,000 |
| `risk_policy_overrides.*` | number | the same ranges as the configured policy |
| `policy_id` | strict format | `INS-[0-9A-Z]{4,12}` or `POL-[0-9]{6,12}`, matched end to end |

Three rules hold across the whole contract.

**Numbers are finite or refused.** `float("NaN")` passes a naive numeric check, and Python's JSON
decoder accepts bare `NaN`, `Infinity` and `-Infinity` in a request body. IEEE NaN comparisons are
always False, so a NaN threshold or a NaN day-count silently clears every check — fail-open on
exactly the decision the agent exists to make. Every caller number goes through
`finite_in_range()` / `finite_int_in_range()` (`src/schemas/state.py`), which reject bools,
non-numerics, non-finite values and out-of-range magnitudes, and fail closed with a
field-naming error. Thresholds and configuration tables are covered by the same rule, not just the
obvious amount fields.

**Strings that render are inert.** Anything a caller supplies that reaches the document must match
`[a-z][a-z0-9_]{0,31}`. Free text there would let the caller write part of the briefing. The
leading-letter requirement is not cosmetic: it makes a purely numeric identifier unrepresentable,
which matters because the output boundary reads long digit runs as monetary figures.

**Rejections name the field, not the value.** An error log is a surface a payload can reach;
repeating it there moves the problem rather than stopping it.

## Outcome contract — what a rejection does to the run

A rejection never produces a briefing. What differs between rejections is whether the run ends or
completes, and that difference is what a calling surface sees.

**A fault the caller boundary can name completes the run.** An empty request, a request over the
length cap, a context channel carrying a contact identifier, a declared policy identifier that is
not one, and a context structure past the depth or size bound are faults in the request rather than
in the agent. `ValidateInputNode` records the class of fault instead of returning an error status;
no inner node then runs, because `RenewalBriefingGraphNode.execute()` sees the recorded reason and
skips the subgraph rather than producing a second, vaguer reason for the same rejection; and
`SecurityGateOutputNode` publishes one of the fixed sentences in `src/services/failure_message.py`
as the run's output. The run reports SUCCESS, carries no briefing and none of the structured fields,
and the caller can correct the value and send the request again on the same conversation.

The alternative was measured against what a caller actually receives. A run that terminates on a
single mistyped value ends the calling surface's turn and surfaces the type of an exception; what to
correct is then reachable only from the audit trail. Completing with a sentence moves that one fact
onto the surface the caller is reading, and changes nothing else: the request is still not carried
out, no document is produced, and the same audit events are emitted.

The sentence names what to correct and nothing else. It never echoes the rejected value, names an
internal field path, or quotes a gate message — those stay in `error_log`, the internal channel. The
reason code is internal in the same way: it is carried through state so the boundary nodes can act
on it, and `get_output()` does not project it into the response.

**Everything else ends the run in error, with no output at all.** An instruction-override or
prompt-disclosure payload is not a value a caller can reword into acceptance. A briefing the output
boundary withheld is not a document to hand back. A broken internal invariant — a node finding the
customer profile missing from state — is not a request fault at all. The portfolio contract enforced
inside the domain pipeline lands here too: a policy, calendar or override field outside its bounds
stops `AggregateCustomerProfileNode` before the profile is written, the next node has nothing to
grade, and the run ends in error. On any non-success status the backbone routes straight to
`finalize`, so `post_process` never runs and the envelope carries `output: null`.

## Security controls

| Layer | Control | Where |
|---|---|---|
| Trust | Caller must be `VERIFIED_EXTERNAL` at the entry slot; inner nodes run `ANONYMOUS` behind it | `ValidateInputNode`, `src/api/server.py` |
| Input | Instruction-override and prompt-disclosure screens on the query **and** on the context channel | `ValidateInputNode` |
| Input | Contact identifiers (email, phone-shaped) refused on the context channel | `ValidateInputNode` |
| Input | Finite + bounded parsing, entry caps, adapter size cap (256 KB) | `AggregateCustomerProfileNode`, `src/api/server.py` |
| Output | Credential / token / script screen, verbatim caller-text redaction, precision grid, re-scan | `SecurityGateOutputNode` |
| Audit | `emit_trace_event` on every accept, rejection, redaction and snap | all nodes |
| Credentials | None held; secrets come from the platform provider at start-up | `src/api/server.py` |

The framework's own input screen inspects `user_input` only. Text placed in `input_context` would
reach the pipeline unexamined, so the template screens that channel itself rather than assuming a
gate in front of it — and the refusal tests call `execute()` directly, with no wrapper, so they
prove the template's own behaviour.

The screens are anchored on both ends and require complete phrases. An unanchored fragment matches
ordinary domain sentences — "Transact as a settlement agent" contains "act as a" — and a validator
that refuses real work is a worse failure than one that lets a clumsy attack reach the next layer.
`tests/unit/test_nodes.py` probes both directions with real sentences.

## The published figure set

The briefing states its own contract and the output boundary enforces it:

> Monetary figures are **portfolio-level aggregates only**, rounded to the nearest **1,000**. A
> single policy's premium is never rendered.

Two figures qualify: the portfolio's total annual premium, and the indicative annual premium of the
recommended covers taken together. Per-policy premiums are not merely unrendered — they are not
carried past `AggregateCustomerProfileNode`, so a later change to the formatter cannot publish one.

**The rounding grid applies here, and the decision is worth stating.** Rounding is only safe where
the rounded figure cannot contradict something else on the page. A document that printed one
policy's own premium beside a threshold verdict derived from that premium could not round it — the
printed number would disagree with the printed verdict. This briefing does not do that: the risk
grade is derived from the renewal window, the premium movement and the claims count, never from the
premium total, so the total can sit on the grid without contradicting anything. That is why the grid
is applied rather than declared not applicable.

`SecurityGateOutputNode` enforces the grid independently of the formatter that applied it. Monetary
values are identified by **form** (comma-grouped numbers, runs of five or more digits) and by
**currency context** (a three-letter code or a symbol, before or after the value, attached or
separated by horizontal whitespace, signed or unsigned) — never by magnitude. An exemption for small
amounts would exempt exactly the figures that are easiest to attribute to one policyholder.

Two details of the grammar are the result of specific failures and should not be simplified away:

- **Identifier guards.** The grammar reads any standalone three-letter uppercase word as a currency
  marker and any long digit run as an amount, so `POL-880123` would be rewritten to `POL-123,000`
  and `plan_48210` to `plan_48,000` — the boundary renaming the things the briefing exists to name.
  Single-character guards on both ends of the match (`[A-Za-z0-9_-]`) prevent it. Underscore is in
  the class because the inert-identifier alphabet uses it; the guard class must match the alphabet
  a given template actually renders. The guards also mean a structured identifier such as
  `SSN 123-45-6789` survives the snap intact, keeping the shape a pattern scan can still match on.
- **A single-line delimiter.** The marker-to-value delimiter spans horizontal whitespace and at most
  one newline. A `\s*` delimiter spans blank lines, so a three-letter word ending a line binds to
  the number opening the next block and `3. Cash Position` is released as `0. Cash Position` — the
  boundary rewriting document structure instead of rounding a figure.
- **Decimals are matched as one number.** The fraction of a decimal is a standalone digit run, and
  the identifier guards admit it because a decimal point is not an identifier character. A grammar
  that stops at the decimal point snaps the FRACTION in place: `8.512345` is released as
  `8.512,000` and `JPY 1234.56` as `JPY 1,000.56` — neither the true value nor a grid value. Each
  value alternative therefore absorbs an optional decimal part, the value is parsed as a decimal
  rather than an integer, and the decimal point joins the **leading** guard only. It must not join
  the trailing guard, or an amount ending a sentence would escape the grid. The effect is that
  `JPY 1234.56` rounds to `JPY 1,000` as one number, while a ratio or percentage — no currency
  context, too few leading digits — is left byte-identical.

The scan runs **before** the snap and again after. Rewriting digit runs first can break the shape a
credential pattern matches on, leaving the secret in the document and unrecognisable. Verbatim field
redaction is order-independent; a pattern scan is not.

Three further properties of the boundary:

- **The redaction layer walks nested values.** A blocked field is not always a bare string —
  `enriched_context` is a mapping, and caller text can ride a level or two down inside it. A layer
  that inspected only top-level strings would walk past that text and the embedding would ship. The
  walk is depth-bounded, because the structure it walks is shaped by a caller.
- **A figure the grid cannot be applied to withholds the document.** Python refuses to convert
  absurdly long digit strings to and from int, so a pathological run cannot be rounded. Passing it
  through unrounded would put an unenforced figure on the external surface — precisely what the grid
  exists to prevent — so the boundary withholds instead.
- **Withholding replaces the document; it does not merely record a status.** A node returns a
  partial state update, so a field the update omits keeps the value it already had. An update that
  carried only an error status would leave `result` — the briefing as the pipeline produced it,
  before the boundary screened it — sitting in the state the response is built from. Every field
  that can carry the document (`formatted_output`, `result`, `agent_briefing`) is therefore
  replaced together with a fixed withheld notice. The notice is non-empty on purpose: the response
  is assembled as `formatted_output or result`, so an empty string would be falsy, the fallback
  would fire, and the withheld briefing would ship anyway.

The response accessor enforces the same decision a second time, independently: a document is
published only on a run whose status is SUCCESS and which recorded no rejection reason, whatever
the state fields still hold. The
inherited accessor resolves the document with no reference to status, so an agent that simply
mirrors it re-publishes exactly what the boundary refused, one key away from a status saying the
run failed. Either measure contains a refusal on its own; both are present so that a future failure
path that forgets to clear, or a future edit to the accessor, cannot put an unscreened briefing in
front of a caller.

The refusal message names the violated rule and the layer that raised it, and quotes nothing from
the document. That is a hard requirement, not a style preference: the framework scans every value
of a node's state update for credential patterns and raises on a hit, and the raised error is
converted into a bare update that clears nothing. A message echoing what it had just found would
therefore discard the replacement above and release the document it was withholding.

## State schema — `src/schemas/state.py`

A flat `TypedDict`. Compound values are stored as JSON-encoded `Optional[str]` via `to_json()` /
`from_json()`; a bare `dict`/`list` field breaks checkpoint serialisation.

| Field | Type | Producer |
|---|---|---|
| `validated_input` | `str` | ValidateInputNode |
| `policy_id` | `Optional[str]` | ValidateInputNode (bridged into the inner graph) |
| `runtime_policy_json` | `Optional[str]` | DomainWorkflowGraph._extra_initial_state |
| `policy_context_json` | `Optional[str]` | AggregateCustomerProfileNode |
| `renewal_risk_json` | `Optional[str]` | IdentifyRenewalRiskNode |
| `coverage_gaps_json` | `Optional[str]` | DetectCoverageGapsNode |
| `cross_sell_json` | `Optional[str]` | GenerateCrossSellRecommendationsNode |
| `agent_briefing` | `Optional[str]` | FormatAgentBriefingNode |
| `result` | `Optional[str]` | RenewalBriefingGraphNode.merge_output |
| `formatted_output` | `Optional[str]` | SecurityGateOutputNode |

Every domain field is `NotRequired`: the TypedDict must be valid at graph initialisation, before any
node has written a value.

Numbers are re-parsed through the bounded parsers at the point of decision rather than trusted from
the serialized profile. State crosses a JSON boundary between nodes and JSON round-trips `NaN` and
`Infinity` intact, so a figure validated at ingest is checked again where it is compared.

## Data flow (end to end)

```
input + input_context (portfolio, renewal_calendar, risk_policy_overrides)
  │
  ▼  ValidateInputNode (pre_process)     query + context screens, policy_id
  │
  ▼  RenewalBriefingGraphNode (main) → DomainWorkflowGraph
    │   caller frame + resolved policy seeded via _extra_initial_state()
    ├── AggregateCustomerProfileNode        → policy_context_json (validated, aggregated)
    ├── IdentifyRenewalRiskNode             → renewal_risk_json
    ├── DetectCoverageGapsNode              → coverage_gaps_json
    ├── GenerateCrossSellRecommendationsNode→ cross_sell_json
    └── FormatAgentBriefingNode             → agent_briefing
  merge_output → result
  │
  ▼  SecurityGateOutputNode (post_process)  scan → redact → grid → re-scan
formatted_output
  │
  ▼  finalize → response_metadata
```

## Class-name consistency

All three locations reference the same class name `RenewalBriefingCrossSellAgent`:

- `src/graph/graph.py` — `class RenewalBriefingCrossSellAgent(AgentBaseGraph)`
- `config/agent.yaml` — `class: "src.graph.graph.RenewalBriefingCrossSellAgent"`
- `src/api/server.py` — `from src.graph.graph import RenewalBriefingCrossSellAgent`

`src/graph/__init__.py` re-exports the class so the registry's
`getattr(import_module("src.graph"), "RenewalBriefingCrossSellAgent")` resolves. Without the
re-export the package still imports and the tests still pass — only a real registry load fails.

## Extensibility

- **Different renewal practice**: edit `config/config.yaml`. Thresholds and catalog are versioned
  configuration; the briefing states the version it applied.
- **Different portfolio shape**: extend the contract in `AggregateCustomerProfileNode`, keeping
  every numeric on the bounded parser and every rendered string inert.
- **A real data source**: replace the caller-supplied portfolio with a service call in the same
  node; the validation contract is the boundary either way.
- **A model in the loop**: this template is deterministic by design. Introducing a model changes
  `generation_mode` in the manifest and the reproducibility claim in this document — both are part
  of the contract, not incidental.
