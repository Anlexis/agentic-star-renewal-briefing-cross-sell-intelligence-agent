"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, the gateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import RenewalBriefingCrossSellAgent

app = FastAPI(title="Agent")

agent = RenewalBriefingCrossSellAgent()
agent.compile()
# The namespace matches the `namespace` declared in config/agent.yaml; the
# agent name matches the registered class.
agent.provision_secrets(secrets_factory(namespace="ins", agent_name="RenewalBriefingCrossSellAgent"))

# Upper bound on the caller-data payload, applied here at the adapter so an
# oversized body is refused before it reaches the graph. The domain contract
# applies its own per-field entry caps once the payload is inside.
_MAX_CONTEXT_BYTES = 256 * 1024


class InvokeRequest(BaseModel):
    """POST /invoke body.

    `input_context` carries the caller's portfolio data: the policy list, the
    renewal calendar and any per-request threshold override. Every field of it
    is optional and every field is validated against explicit bounds inside the
    pipeline; an absent context yields the empty-portfolio baseline briefing
    rather than an error.
    """

    input: str
    session_id: str = ""
    input_context: Dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Trust established by middleware is never demoted.
    # This adapter is the entry-point auth boundary — a deployment-level caller
    # credential rather than an agent secret, so the secrets provider does not
    # apply here: no invocation context exists before authentication.
    #
    # Required specifically because ValidateInputNode
    # (src/nodes/validate_input.py) occupies the pre_process slot and declares
    # required_trust_level = TrustLevel.VERIFIED_EXTERNAL. Nothing else sets
    # request.state.trust_level in a standalone deployment, so without this
    # boundary every invocation arrives ANONYMOUS, the trust gate denies it,
    # and the agent returns status="error".
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was
            # absent, malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context = req.input_context or {}
    if input_context:
        try:
            payload_bytes = len(json.dumps(input_context).encode("utf-8"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="input_context must be a JSON object.")
        if payload_bytes > _MAX_CONTEXT_BYTES:
            raise HTTPException(status_code=413, detail="input_context exceeds the accepted size.")

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=input_context)
        return result


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "RenewalBriefingCrossSellAgent"}
