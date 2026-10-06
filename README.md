# Renewal Briefing & Cross-Sell Intelligence Agent

AI agent for briefing insurance agents on renewals and cross-sell opportunities, built with Agentic Star.

> **Category**: Cat 2 (domain pipeline — document generation)
> **Industry**: Insurance
> **Template ID**: INS-C2-018

## Overview

Turns one policyholder's book into a renewal briefing an insurance agent can walk into a call
with: the renewal risk grade and the factors behind it, the cover categories the portfolio does
not hold, and ranked cross-sell talking points sized by indicative annual premium.

Rendering is deterministic. No model is called, so the same book always produces the same
briefing. What the briefing publishes is deliberately narrow: monetary figures are
portfolio-level aggregates rounded to the nearest 1,000, never a single policy's premium, and
the output boundary re-checks that rule on the finished document rather than trusting the
formatter that applied it. Every caller-supplied string that reaches the page is an inert
identifier, so nothing a caller sends can write prose into the document.

The grading thresholds are configuration — an agency's commercial policy, not a statutory rule —
so adapting the agent to another agency's renewal practice is a change to `config/config.yaml`,
not to code, and the briefing states which policy version produced its verdict.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas, sample walkthroughs)
tests/        unit and boundary tests
config/       agent.yaml (registration manifest) + config.yaml (runtime parameters)
docs/         design and test documentation
```

`docs/02_design.md` describes the architecture, the caller-data contract and the published
figure set; `docs/03_test_spec.md` maps each of those guarantees to the test that holds it.
`src/examples/` walks the framework patterns using this repository's own code.

## Customising

1. Set your own renewal thresholds and cross-sell catalog in `config/config.yaml`. Values are
   bounds-checked at load; anything unusable falls back to the documented default and the
   briefing reports the policy as not fully applied rather than grading against it silently.
2. Adjust the caller-data contract in `src/nodes/aggregate_customer_profile.py` if your
   portfolio records carry different fields — keep every numeric field on the finite +
   bounded parser and every rendered string on the inert identifier form.
3. Adapt the briefing layout in `src/nodes/format_agent_briefing.py`. If you render a new
   monetary figure, keep it an aggregate: the output boundary enforces the rounding grid, but
   only the formatter decides what is published at all.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
