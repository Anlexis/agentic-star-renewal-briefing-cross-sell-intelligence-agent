# Test Specification — INS-C2-018 Renewal Briefing & Cross-Sell Agent

## Strategy

Two layers, with different jobs.

**Unit tests** (`tests/unit/`) call `node.execute()` directly. For the refusal cases that is
deliberate rather than convenient: a refusal asserted through a wrapper might be the wrapper's, and
a test that passes because something upstream happened to reject the payload proves nothing about
this template. Assertions are behavioural — the run's status and the absence of the node's output
keys — never the exact wording of a message, so a framework release that rephrases an error does not
turn into a red suite. Two outcomes are asserted, and the refusal cases are split between them: a
fault the caller can correct completes the run (`SUCCESS`, no output keys, the class of fault in
`error_log`), while an instruction-override payload or a broken invariant ends it (`ERROR`, no
output keys). See `docs/02_design.md` § Outcome contract.

**Boundary tests** (`tests/proof_of_boundary/`) drive the compiled agent through its real ASGI
entry point. One guarantee can only be established there: the nested-graph boundary does not
forward `input_context` to the inner graph, so an agent can pass every node-level test while its
public path silently ignores caller data and returns the same baseline document to everyone. Only
an end-to-end assertion on a caller-derived figure catches that.

The app is driven through its raw ASGI interface rather than through a test client, so the boundary
tests cannot silently skip on a missing optional dependency.

The pipeline is deterministic and offline: no model, no network, no external service. The same
input always produces the same briefing.

## Framework compliance

| TC-ID | Check | Where | Expected |
|---|---|---|---|
| TC-01 | `State` is a flat `TypedDict`; compound values are JSON-encoded `Optional[str]` | `tests/proof_of_boundary/test_state_safety.py` | 0 model/dataclass fields |
| TC-02 | No platform-internal imports under `src/` | `tests/proof_of_boundary/test_import_isolation.py` | 0 violations |
| TC-03 | `_security_gate_input()` cannot be overridden on a `FunctionNode` subclass | `tests/unit/test_framework_compliance_tc06_tc07.py` | `TypeError` at class definition |
| TC-04 | `_security_gate_output()` cannot be overridden on a `FunctionNode` subclass | `tests/unit/test_framework_compliance_tc06_tc07.py` | `TypeError` at class definition |
| TC-05 | Every node declares `required_trust_level`; entry slot is `VERIFIED_EXTERNAL`, the rest `ANONYMOUS` | `tests/unit/test_trust_gate.py::TestTrustLevelMatrix` | matches the declared matrix |
| TC-06 | Class name agrees across the graph module, the manifest and the entry point | `docs/02_design.md` § Class-name consistency; import in `src/api/server.py` | identical strings |
| TC-07 | Inner nodes are constructed with no arguments | `src/graph/domain_workflow_graph.py::register_nodes` | no constructor arguments |

## Caller boundary — `tests/unit/test_nodes.py`

### The query and context screens (`TestValidateInputNode`)

| Case | Input | Expected |
|---|---|---|
| Clean query | ordinary request text | `SUCCESS`, `validated_input` trimmed |
| Empty / whitespace | `""`, `"   "` | run completes (`SUCCESS`), no output keys, `error_log` says the input was empty |
| Oversize | 4,097 characters | run completes (`SUCCESS`), `error_log` names the limit |
| Instruction override | 9 attack forms, parametrized (temporal, named-object, disclosure, role reassignment, delimiter, markup, template expression) | `ERROR`, `validated_input` absent |
| Ordinary domain language | 6 real sentences containing the same words the screens look for, including "Transact as a settlement agent" | accepted |
| Injection on the context channel | attack text nested inside `input_context` | `ERROR` naming the field path |
| Contact identifier on the context channel | an email address in a context field | run completes (`SUCCESS`), `error_log` names the class of fault |
| Rejection hygiene | a refused instruction-override payload | the payload does not appear in the error |
| Context depth | 12 levels of nesting | the bounded traversal returns a reason rather than walking on |
| Policy identifier | parsed from text; declared in context; malformed in context; absent | precedence, strict end-to-end match, rejection, `None` |

### The finite + bounded parsers (`TestFiniteParsers`)

Parametrized over the full non-finite matrix — the JSON strings a client can send (`"NaN"`,
`"Infinity"`, `"-Infinity"`) and the Python floats a decoder produces from them — plus bools,
non-numerics, out-of-range magnitudes and fractional values where integrality is required. Also
covers the inert identifier form in both directions, including the case that a purely numeric
identifier is refused.

### The caller-data contract (`TestCallerDataContract`)

| Case | Expected |
|---|---|
| A two-policy portfolio | aggregated counts, totals and categories |
| No context at all | the empty-portfolio baseline, `SUCCESS` |
| Per-policy premiums | never carried past this node; only the total is |
| Non-finite premium / claims count | rejected, naming the field, per field, per spelling; `policy_context_json` absent |
| Non-finite calendar figure | rejected, naming the field |
| Non-finite threshold override | rejected — thresholds are caller numbers too |
| Over-magnitude / negative premium | rejected |
| More than 50 policies | rejected, naming the cap |
| Free text in a rendered field | rejected, naming the field |
| Rejection hygiene | the value never appears in the error |
| Accepted override | reaches the profile and is listed as overridden |

Each rejection here stops the node before the profile is written, so the row's assertion is that
`policy_context_json` is absent and the field is named. End to end the run still terminates: the
next node has no profile to grade and fails the invariant, which is why
`tests/proof_of_boundary/test_pb_invoke_endpoint.py` asserts `ERROR` and no output for the same
payloads.

### The graded decision (`TestRenewalRisk`)

Zero factors grade LOW, one grades MEDIUM, two grade HIGH, an empty portfolio grades HIGH; the
threshold boundary is inclusive on both sides; an accepted override moves the verdict; a non-finite
figure surviving into serialized state is refused at the point of decision and no
`renewal_risk_json` is written; a non-finite *threshold* falls back to the documented default rather
than being compared against.

### Gaps, ranking and rendering (`TestCoverageGapsAndCrossSell`, `TestBriefingRendering`)

Gaps are the configured catalog minus the held cover; full cover yields none. Recommendations rank
by opportunity, deterministically, and the opportunity total is the sum of the recommended covers'
indicative premiums. The briefing names the policy, the reference and the verdict; renders
aggregates on the published grid; states its own figure contract and its policy provenance; and
renders an unusable aggregate as "undetermined" rather than as a zero a reader would take for a
fact.

## Output boundary — `tests/unit/test_output_boundary.py`

| Group | What it holds |
|---|---|
| `TestGridIsEnforcedForEveryRepresentation` | 19 representations snap: comma-grouped at any magnitude, five-or-more-digit runs, short values in currency context, marker before or after, code or symbol (including fullwidth), signed, and separated by arbitrary horizontal whitespace or one newline. Plus decimal amounts, which round as one number rather than having their fraction rewritten. Plus the grouped-adjacency case: `JPY 1,000` stays byte-identical and `JPY 1,234` snaps as 1234, not as 1. |
| `TestStructuralTokensSurvive` | 12 tokens are byte-identical: policy identifiers, inert caller identifiers including a five-digit tail, day counts, percentages, years, version tags, embedded acronyms, on-grid amounts. Plus a section heading following a three-letter code, which a paragraph-spanning delimiter would renumber. Plus the complement: a purely numeric identifier is refused at input, because nothing adjacent would guard it. |
| `TestDecimalsAreNotRewritten` | A fraction is never snapped in place — ratios, percentages, the rendered policy version and the effective date all stay byte-identical; an on-grid decimal amount is unchanged; an off-grid one rounds as a whole number; and an amount ending a sentence is still caught, which is why the decimal point is in the leading guard only. |
| `TestLayerOrder` | The scan reads the intact document; the snap does not rewrite a credential before it is scanned; a hyphenated personal number survives the snap with the shape a pattern scan matches on; six blocked pattern classes are named; a clean briefing passes. |
| `TestVerbatimCallerTextRedaction` | A verbatim embedding of the caller's query is replaced; a short incidental overlap is not; caller text nested inside a blocked mapping is found (with the top-level case as the control that proves the probe tests the walk); the walk is depth-bounded. |
| `TestOutputNodeBehaviour` | A clean briefing is released unchanged; an off-grid figure is snapped on release; a blocked or oversize briefing is withheld **entirely** (no partially sanitised release); a figure the grid cannot be applied to withholds rather than passing through unrounded; the rejection never echoes the content. |
| `TestWithholdingClearsTheDocument` | Withholding **replaces** every field that could still be carrying the briefing (`formatted_output`, `result`, `agent_briefing`) rather than leaving them at their previous values; the replacement is truthy, because a falsy one re-activates the `formatted_output or result` fallback; the withheld document appears nowhere in the update; every violation class withholds the same way; and the replacement survives the framework's own credential scan over the update — the proof that the refusal message names the violated rule instead of quoting what it found. |
| `TestTheEnvelopeReleasesOnlyOnSuccess` | The response accessor publishes a document only on a SUCCESS run: parametrized over error, timeout, cancelled and pending, a state still holding the full briefing in both `formatted_output` and `result` yields no document; the `result` fallback specifically is gone on a failed run; and the control — a successful run still publishes the released briefing. |

## Runtime configuration — `tests/unit/test_runtime_config.py`

The failure this file exists to prevent is silent: a graph built with an empty config runs on
in-code defaults, every test passes, and editing `config/config.yaml` changes nothing.

| Group | What it holds |
|---|---|
| `TestRuntimeConfigIsRead` | The file declares the policy; the agent constructor defaults to it; the parent forwards it under `configurable` with `timeout_s` also exposed as `timeout_seconds`; the inner graph resolves it; the resolved policy and the caller frame are seeded into the inner initial state. |
| `TestPolicyResolutionBounds` | Absent configuration falls back and flags itself; an unusable threshold (parametrized over non-finite and out-of-range values) keeps the documented default and marks the policy not effective; a usable one is applied; a catalog key that is not an inert identifier or a premium outside its range is dropped; an empty catalog falls back. |
| `TestCallerFrameIsolation` | An unset frame reads as empty; the frame copies rather than aliases the caller's mapping. |

## Boundary tests — `tests/proof_of_boundary/`

| File | Boundary | Expected |
|---|---|---|
| `test_pb_invoke_endpoint.py` | The real `POST /invoke`, authenticated, with caller data | Caller-derived figures appear in the briefing (two policies, the aggregated total on the grid) and the raw line items do not, at any precision; the verdict follows the data (HIGH and LOW cases); absent caller data degrades to the baseline; a whole-document scan finds no off-grid monetary figure; unbounded, free-text and injection payloads end the run in error with no output; an oversize context is refused at the adapter (413); an unauthenticated caller is refused (401); and a briefing refused by the output boundary is absent from the error envelope — no document structure, figures, caller identifiers, traceback or source paths — with the same request on a passing boundary as the control |
| `test_server_boot.py` | The entry-point auth boundary | Token configured + absent/wrong bearer → 401 with a generic body; correct bearer → the context is built at `VERIFIED_EXTERNAL`; no token configured → the caller stays unauthenticated; trust established upstream is never demoted |
| `test_pb_invoke_order.py` | Backbone order and per-node call order | A valid payload runs all backbone slots to a non-empty output; every node runs trust gate → node_start → execute → output screen → node_complete |
| `test_import_isolation.py` | No platform-internal imports under `src/` | 0 violations |
| `test_state_safety.py` | State field types | Checkpoint-safe fields only |
| `test_pb7_hitl_interrupt_propagation.py` | Human-in-the-loop propagation | Skipped with a stated reason — this agent sets `propagate_hitl = False` and raises no interrupt |

## Running the tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

The suite runs offline and requires no platform connection. Running the agent itself does.

## Known limitations

- The portfolio is supplied by the caller; there is no integration against a live policy
  administration system. The validation contract is the boundary either way, so swapping the
  source does not move the guarantees — but nothing here exercises a real one.
- Indicative premiums in the cross-sell catalog are planning figures, not quotes, and no test
  asserts they match any carrier's rate table.
- The briefing's Japanese prose is fixed template text; no test asserts its readability.
- Nothing asserts the caller-facing sentence a completed rejection publishes, or the reason code
  that selects it. The refusal cases assert the run's status and that the node's output keys are
  absent, which is the property that must not regress; the wording is deliberately outside the
  assertions.
