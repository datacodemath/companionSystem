"""Protocol smoke check; prints only status/counts, never companion context."""
import json
import sys
import urllib.error
import urllib.request

from fastapi.testclient import TestClient
from profile_os.mcp_server import MCPSettings, create_mcp_app


def check(request, token, *, live=False):
    base_headers = {"Authorization": f"Bearer {token}"}
    count = 0
    for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
        for accept in ("application/json", "application/json, text/event-stream"):
            headers = {**base_headers, "MCP-Protocol-Version": version, "Accept": accept}
            def rpc(method, params=None):
                status, _, body = request("POST", headers, {
                    "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
                })
                assert status == 200, status
                if body.startswith(b"event:"):
                    body = next(line[6:] for line in body.splitlines() if line.startswith(b"data: "))
                result = json.loads(body)
                assert "error" not in result
                return result["result"]

            assert rpc("initialize", {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "agy-transport-smoke", "version": "1"}})["protocolVersion"] == version
            status, _, body = request("POST", headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert status == 202 and not body
            status, response_headers, body = request("GET", {**headers, "Accept": "text/event-stream"})
            assert status == 405 and response_headers.get("allow") == "POST" and not body
            tools = rpc("tools/list")["tools"]
            assert any(t["name"] == "summon_companion" for t in tools)
            count = len(tools)
            if live and version == "2025-11-25":
                result = rpc("tools/call", {"name": "summon_companion", "arguments": {"profile_id": "limo"}})
                assert not result.get("isError"), "summon failed"
                content = result.get("structuredContent")
                if content is None:
                    content = json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
                assert content["selection"]["profile_id"] == "limo"
    assert request("GET", {"Accept": "text/event-stream"})[0] == 401
    assert request("GET", {**base_headers, "Origin": "https://evil.example"})[0] == 403
    assert request("GET", {**base_headers, "MCP-Protocol-Version": "2099-01-01"})[0] == 400
    print(json.dumps({"passed": True, "versions": 3, "post_formats": 2,
                      "tool_count": count, "summon_limo": live}))


if "--live" in sys.argv:
    settings = MCPSettings.from_env()
    def request(method, headers, payload=None):
        req = urllib.request.Request("https://rumbo.datacodemath.com/mcp", method=method,
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(payload).encode() if payload is not None else None)
        try:
            response = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    check(request, settings.connector_tokens[0], live=True)
else:
    settings = MCPSettings(auth_required=True, connector_tokens=["transport-test"],
        allowed_origins=["https://chatgpt.com"], oauth_signing_key="test-signing-key")
    with TestClient(create_mcp_app(settings=settings)) as client:
        def request(method, headers, payload=None):
            response = client.request(method, "/mcp", headers=headers,
                **({"json": payload} if payload is not None else {}))
            return response.status_code, response.headers, response.content
        check(request, "transport-test")
