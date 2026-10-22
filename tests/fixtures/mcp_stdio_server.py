"""Deliberately tiny JSON-RPC fixture: never deploy as a real MCP server."""

from __future__ import annotations

import argparse
import json
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--era", choices=("modern", "legacy"), default="modern")
parser.add_argument("--scenario", default="normal")
args = parser.parse_args()


def reply(req, *, result=None, error=None, reply_id=None):
    row = {"jsonrpc": "2.0", "id": req["id"] if reply_id is None else reply_id}
    row.update({"error": error} if error is not None else {"result": result})
    print(json.dumps(row, separators=(",", ":")), flush=True)


def tool(name):
    return {
        "name": name,
        "description": "Adds two integers (declared read only, not trusted)",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    }


for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    if method == "server/discover":
        if args.era == "legacy":
            reply(req, error={"code": -32601, "message": "Method not found"})
        else:
            reply(
                req,
                result={
                    "resultType": "complete",
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                },
            )
    elif method == "initialize":
        reply(
            req,
            result={
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            },
        )
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        if args.scenario == "cursor_cycle":
            reply(req, result={"tools": [tool("add")], "nextCursor": "same"})
        elif args.scenario == "duplicates":
            reply(req, result={"tools": [tool("add"), tool("add")]})
        elif args.scenario == "pages":
            if req.get("params", {}).get("cursor") == "second":
                reply(req, result={"tools": [tool("multiply")]})
            else:
                reply(req, result={"tools": [tool("add")], "nextCursor": "second"})
        elif args.scenario == "bad_schema":
            broken = tool("add")
            broken["inputSchema"] = {"type": "object", "properties": {"x": {"type": "imaginary"}}}
            reply(req, result={"tools": [broken]})
        else:
            reply(req, result={"tools": [tool("add")]})
    elif method == "tools/call":
        if args.scenario == "crash":
            sys.exit(3)
        if args.scenario == "timeout":
            time.sleep(1.0)
        if args.scenario == "wrong_id":
            reply(req, result={"content": []}, reply_id=999)
            continue
        if args.scenario == "oversize":
            reply(
                req, result={"content": [{"type": "text", "text": "x" * (2 * 1024 * 1024 + 1024)}]}
            )
            continue
        if args.scenario == "server_request":
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 712, "method": "sampling/createMessage", "params": {}}
                ),
                flush=True,
            )
        if args.scenario == "notify":
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/message",
                        "params": {"level": "info"},
                    }
                ),
                flush=True,
            )
        if args.scenario == "tool_error":
            reply(req, result={"content": [{"type": "text", "text": "failure"}], "isError": True})
        elif args.scenario == "input_required":
            reply(
                req,
                result={
                    "resultType": "input_required",
                    "inputRequests": [{"method": "elicitation/create"}],
                },
            )
        else:
            payload = req["params"]["arguments"]
            total = payload["a"] + payload["b"]
            reply(
                req,
                result={
                    "resultType": "complete",
                    "content": [{"type": "text", "text": str(total)}],
                    "structuredContent": {"sum": total},
                },
            )
    elif "id" in req:
        reply(req, error={"code": -32601, "message": "Unknown method"})
