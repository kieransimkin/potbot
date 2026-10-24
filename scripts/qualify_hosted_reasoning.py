#!/usr/bin/env python3
"""Opt-in paid OpenAI/Gemini text reasoning smoke qualification.

Never automatically invoked by CI. Explicitly requires paid-call and disclosure
approval; never logs credentials or returned answer text. Tests a real provider
response, not the existence of a model name in a catalogue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from potbot.domain.errors import InvalidInputError
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.providers import (
    HostedProviderClient,
    ProviderCompleted,
    ProviderRequest,
    ProviderSelection,
    ProviderTextDelta,
)
from potbot.service_credentials import ServiceCredentials

_PROMPT = 'Reply with exactly this JSON object: {"qualification":"ok"}'
_SYSTEM = "Return a valid JSON object and do not use a tool or reveal private deliberation."


def qualification_request(provider: str, model: str, policy: HostedPolicy) -> ProviderRequest:
    if provider not in {"openai", "gemini"}:
        raise InvalidInputError("Smoke qualification supports only native OpenAI and Gemini")
    rule = policy.rule(provider, model, "reasoning")
    request = ProviderRequest(
        ProviderSelection(provider, model, rule.protocol),
        prompt=_PROMPT,
        system=_SYSTEM,
        max_output_tokens=128,
        timeout_seconds=min(90, policy.max_timeout_seconds),
        response_format="json_object",
    )
    policy.authorize(request)
    estimate = rule.charge(len((_PROMPT + _SYSTEM).encode()), request.max_output_tokens)
    if estimate > policy.max_cost_microunits:
        raise InvalidInputError("Qualification exceeds operator cost ceiling")
    return request


def qualify(
    request: ProviderRequest, policy: HostedPolicy, credentials: Any, *,
    client_factory: Any = HostedProviderClient,
) -> dict[str, Any]:
    """Exactly one provider call; no retries and no credential/answer logging."""
    client = client_factory(credentials.resolve, authorize=policy.authorize)
    pieces: list[str] = []
    completed: ProviderCompleted | None = None
    started = time.monotonic()
    try:
        for event in client.stream(request, uuid4()):
            if isinstance(event, ProviderTextDelta):
                pieces.append(event.text)
            elif isinstance(event, ProviderCompleted):
                completed = event
    finally:
        client.close()
    content = "".join(pieces)
    if completed is None or completed.finish_reason != "stop" or completed.usage is None:
        raise RuntimeError("Provider did not return complete text and usage")
    try:
        record = json.loads(content)
    except (ValueError, TypeError):
        record = None
    return {
        "schema_version": 1,
        "provider": request.selection.provider,
        "model": request.selection.model,
        "protocol": request.selection.protocol,
        "result": "pass" if record == {"qualification": "ok"} else "content_mismatch",
        "finish_reason": completed.finish_reason,
        "usage": completed.usage.to_dict(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "prompt_sha256": hashlib.sha256((_SYSTEM + "\n" + _PROMPT).encode()).hexdigest(),
        "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "returned_model": completed.model,
        "qualified_for_production": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("openai", "gemini"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider-policy", required=True, type=Path)
    parser.add_argument("--allow-paid", action="store_true",
                        help="explicitly authorize one provider request")
    parser.add_argument("--confirm-prompt-disclosure", action="store_true",
                        help="allow the fixed synthetic test prompt to leave the host")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        policy = HostedPolicy.read(args.provider_policy)
        request = qualification_request(args.provider, args.model, policy)
        if not args.allow_paid or not args.confirm_prompt_disclosure:
            print(json.dumps({"status": "preflight_only", "provider": args.provider,
                              "model": args.model, "protocol": request.selection.protocol,
                              "paid_request_sent": False}))
            return 0
        report = qualify(request, policy, ServiceCredentials())
        serialized = json.dumps(report, indent=2, sort_keys=True)
        if args.report is not None:
            path = args.report.resolve()
            if path.exists():
                raise InvalidInputError("Report target already exists; never overwrite a receipt")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        print(serialized)
        return 0 if report["result"] == "pass" else 1
    except Exception:
        # Do not print provider exception details: some include response headers.
        print(json.dumps({"status": "failed", "provider": args.provider,
                          "model": args.model, "details": "See operator diagnostics"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
