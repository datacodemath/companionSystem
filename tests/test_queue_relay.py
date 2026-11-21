import base64
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest

from profile_os.mcp_server import (
    MCPSettings,
    QUEUE_MCP_TOOLS,
    QueueCallerBinding,
    QueueWorkerBinding,
    create_mcp_app,
)
from profile_os.queue_relay import MAX_BODY
from test_mcp_server import FakeBridge, ORIGIN, PUBLIC_BASE, _rpc


CHAT1_TOKEN = "owner-andres-chat-1-secret"
CHAT2_TOKEN = "owner-andres-chat-2-secret"
OTHER_OWNER_TOKEN = "owner-other-secret"
UNPRIVILEGED_MCP_TOKEN = "connector-no-queue-secret"
WORKER_YOGA_TOKEN = "worker-yoga-route-secret"
WORKER_OTHER_TOKEN = "worker-other-route-secret"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _headers(token: str | None = None, *, origin: str | None = ORIGIN) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _call_mcp(client: TestClient, token: str | None, method: str, params: dict | None = None, req_id: int = 1):
    return client.post(
        "/mcp",
        json=_rpc(method, params, req_id),
        headers=_headers(token),
    )


def _call_tool(client: TestClient, token: str, name: str, arguments: dict, req_id: int = 10):
    return _call_mcp(client, token, "tools/call", {"name": name, "arguments": arguments}, req_id)


def _make_settings(tmp_path: Path, **overrides) -> MCPSettings:
    defaults = dict(
        auth_required=True,
        connector_tokens=[UNPRIVILEGED_MCP_TOKEN],
        allowed_origins=[ORIGIN],
        public_base_url=PUBLIC_BASE,
        oauth_issuer=PUBLIC_BASE,
        oauth_signing_key="test-queue-oauth-signing-key",
        closeout_code_state_file=str(tmp_path / "closeout.sqlite3"),
        queue_db_path=str(tmp_path / "queue-relay.sqlite3"),
        queue_default_route="yoga-primary",
        queue_callers=[
            QueueCallerBinding(
                token=CHAT1_TOKEN,
                owner="andres",
                route="yoga-primary",
                label="chat-session-1",
            ),
            QueueCallerBinding(
                token=CHAT2_TOKEN,
                owner="andres",
                route="yoga-primary",
                label="chat-session-2",
            ),
            QueueCallerBinding(
                token=OTHER_OWNER_TOKEN,
                owner="other-owner",
                route="other-route",
                label="other-owner-chat",
            ),
        ],
        queue_workers=[
            QueueWorkerBinding(
                token=WORKER_YOGA_TOKEN,
                route="yoga-primary",
                worker_id="yoga-worker",
            ),
            QueueWorkerBinding(
                token=WORKER_OTHER_TOKEN,
                route="other-route",
                worker_id="other-worker",
            ),
        ],
    )
    defaults.update(overrides)
    return MCPSettings(**defaults)


def _make_client(tmp_path: Path, *, clock=None, **settings_overrides) -> TestClient:
    settings = _make_settings(tmp_path, **settings_overrides)
    app = create_mcp_app(
        bridge=FakeBridge(),
        settings=settings,
        queue_clock=clock,
    )
    return TestClient(app)


def _oauth_mint_token(client: TestClient, client_name: str) -> tuple[str, str]:
    reg = client.post("/oauth/register", json={
        "client_name": client_name,
        "redirect_uris": ["https://claude.ai/oauth/callback"],
    })
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]
    verifier = f"verifier-{client_id}-" + ("X" * 32)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    redirect_uri = "https://claude.ai/oauth/callback"
    authz = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
            "resource": f"{PUBLIC_BASE}/mcp",
            "admin_secret": "root-secret",
            "totp_code": "123456",
        },
        follow_redirects=False,
    )
    assert authz.status_code == 303
    code = urllib.parse.parse_qs(urllib.parse.urlparse(authz.headers["location"]).query)["code"][0]
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    })
    assert tok.status_code == 200
    return client_id, tok.json()["access_token"]


def test_tool_discovery_and_output_schemas(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_OMIT_OUTPUT_SCHEMAS", "0")
    with _make_client(tmp_path) as client:
        # Authorized caller sees queue_task and get_task.
        r = _call_mcp(client, CHAT1_TOKEN, "tools/list")
        assert r.status_code == 200
        tools = {tool["name"]: tool for tool in r.json()["result"]["tools"]}
        assert {"queue_task", "get_task"} <= set(tools)
        assert tools["get_task"]["annotations"]["readOnlyHint"] is True
        assert tools["queue_task"]["annotations"]["idempotentHint"] is True

        # Authenticated caller without queue permission does NOT see queue tools.
        r_unpriv = _call_mcp(client, UNPRIVILEGED_MCP_TOKEN, "tools/list")
        assert r_unpriv.status_code == 200
        unpriv_names = {tool["name"] for tool in r_unpriv.json()["result"]["tools"]}
        assert not (unpriv_names & {"queue_task", "get_task"})

        # Output schemas validate real structuredContent responses.
        schema_by_name = {tool["name"]: tool["outputSchema"] for tool in QUEUE_MCP_TOOLS}
        submitted = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "schema-check-1",
            "task": "Verify schema compliance",
            "companion": "limo",
            "requester": "siro",
            "executor": "limo",
        }).json()["result"]
        assert submitted["isError"] is False
        Draft202012Validator(schema_by_name["queue_task"]).validate(submitted["structuredContent"])

        fetched = _call_tool(client, CHAT2_TOKEN, "get_task", {
            "job_id": submitted["structuredContent"]["job_id"],
        }).json()["result"]
        assert fetched["isError"] is False
        Draft202012Validator(schema_by_name["get_task"]).validate(fetched["structuredContent"])


def test_round_trip_dedup_cross_chat_receipt_and_attribution(tmp_path):
    now = [1_000.0]
    with _make_client(tmp_path, clock=lambda: now[0]) as client:
        # 1. Companion in Chat 1 submits a task with companion attribution labels.
        submit1 = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "smoke-2026-11-21-01",
            "task": "Inspect status and emit DONE Queue API round-trip",
            "backend": "codex",
            "priority": 5,
            "companion": "limo",
            "requester": "siro",
            "executor": "limo",
            "task_kind": "review",
        }).json()["result"]
        assert submit1["isError"] is False
        rec1 = submit1["structuredContent"]
        job_id = rec1["job_id"]
        assert job_id.startswith("qjob_")
        assert rec1["request_id"] == "smoke-2026-11-21-01"
        assert rec1["delivery_state"] == "pending"
        assert rec1["state"] is None
        assert rec1["worker_job_id"] is None
        assert rec1["companion"] == "limo"
        assert rec1["requester"] == "siro"
        assert rec1["executor"] == "limo"
        assert "task" not in rec1
        assert "bridge_id" not in rec1

        # 2. Retry the exact same request from Chat 2 (including explicit default fields).
        submit2 = _call_tool(client, CHAT2_TOKEN, "queue_task", {
            "request_id": "smoke-2026-11-21-01",
            "task": "Inspect status and emit DONE Queue API round-trip",
            "backend": "codex",
            "model": None,
            "priority": 5,
            "codex_fallback_authorized": False,
            "delegation_mode": "queue_only",
            "companion": "limo",
            "requester": "siro",
            "executor": "limo",
            "task_kind": "review",
        }).json()["result"]
        assert submit2["isError"] is False
        assert submit2["structuredContent"]["job_id"] == job_id
        assert submit2["structuredContent"] == rec1

        # Conflicting companion and executor labels fail fast at intake (400).
        bad_labels = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "bad-labels-1",
            "task": "Invalid companion/executor pair",
            "companion": "siro",
            "executor": "limo",
        }).json()["result"]
        assert bad_labels["isError"] is True
        assert bad_labels["structuredContent"]["error"]["status"] == 400

        # 3. Changed payload with same request_id conflicts (409).
        conflict = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "smoke-2026-11-21-01",
            "task": "Different task text",
        }).json()["result"]
        assert conflict["isError"] is True
        assert conflict["structuredContent"]["error"]["status"] == 409

        # 4. Yoga worker pulls outbound under its separate worker credential.
        claim_resp = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 8},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        )
        assert claim_resp.status_code == 200
        claim_body = claim_resp.json()
        assert claim_body["version"] == 1
        assert len(claim_body["claims"]) == 1
        assert claim_body["bindings"] == []
        claim = claim_body["claims"][0]
        assert claim["request"]["task"] == "Inspect status and emit DONE Queue API round-trip"
        assert claim["request"]["companion"] == "limo"
        assert claim["request"]["requester"] == "siro"
        assert claim["request"]["executor"] == "limo"

        # A second claim while leased yields zero claims (runs only once!).
        second_claim = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 8},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        )
        assert second_claim.status_code == 200
        assert second_claim.json()["claims"] == []

        # 5. Worker acks local commit (and repeating the exact ack is idempotent).
        ack_payload = {
            "version": 1,
            "bridge_id": claim["bridge_id"],
            "lease_id": claim["lease_id"],
            "job_id": 42,
        }
        assert client.post(
            "/queue/worker/ack",
            json=ack_payload,
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json() == {"version": 1, "ok": True}
        assert client.post(
            "/queue/worker/ack",
            json=ack_payload,
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json() == {"version": 1, "ok": True}

        # 6. Next poll offers the delivered non-terminal binding for status sync.
        poll2 = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 8},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json()
        assert poll2["claims"] == []
        assert poll2["bindings"] == [{"bridge_id": claim["bridge_id"], "job_id": 42}]

        # 7. Worker publishes running status, then terminal done receipt.
        now[0] = 1_010.0
        running_snap = {
            "id": 42, "priority": 5, "state": "running", "attempts": 1,
            "result": None, "finished": None,
        }
        assert client.post(
            "/queue/worker/status",
            json={"version": 1, "bridge_id": claim["bridge_id"], "job_id": 42, "snapshot": running_snap},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).status_code == 200

        now[0] = 1_150.0  # > 120s stale window while unfinished
        stale_read = _call_tool(client, CHAT2_TOKEN, "get_task", {"job_id": job_id}).json()["result"]["structuredContent"]
        assert stale_read["state"] == "running"
        assert stale_read["stale"] is True

        now[0] = 1_160.0
        done_snap = {
            "id": 42, "priority": 5, "state": "done", "attempts": 1,
            "result": {"receipt": "DONE Queue API round-trip"},
            "finished": now[0],
        }
        assert client.post(
            "/queue/worker/status",
            json={"version": 1, "bridge_id": claim["bridge_id"], "job_id": 42, "snapshot": done_snap},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).status_code == 200

        # Conflicting update after terminal receipt is rejected with 409.
        tamper = client.post(
            "/queue/worker/status",
            json={
                "version": 1,
                "bridge_id": claim["bridge_id"],
                "job_id": 42,
                "snapshot": {**done_snap, "result": {"receipt": "TAMPERED"}},
            },
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        )
        assert tamper.status_code == 409

        # 8. Another authorized chat retrieves the receipt by job_id AND by request_id.
        by_job_id = _call_tool(client, CHAT2_TOKEN, "get_task", {"job_id": job_id}).json()["result"]["structuredContent"]
        by_req_id = _call_tool(client, CHAT2_TOKEN, "get_task", {"request_id": "smoke-2026-11-21-01"}).json()["result"]["structuredContent"]
        assert by_job_id == by_req_id
        assert by_job_id["job_id"] == job_id
        assert by_job_id["delivery_state"] == "delivered"
        assert by_job_id["state"] == "done"
        assert by_job_id["worker_job_id"] == 42
        assert by_job_id["receipt"] == {"receipt": "DONE Queue API round-trip"}
        assert by_job_id["snapshot"] == done_snap
        assert by_job_id["stale"] is False
        assert by_job_id["companion"] == "limo"
        assert by_job_id["requester"] == "siro"
        assert by_job_id["executor"] == "limo"

        # Terminal job is no longer offered in bindings.
        poll3 = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 8},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json()
        assert poll3 == {"version": 1, "claims": [], "bindings": []}


def test_authorization_boundaries_and_credential_separation(tmp_path):
    with _make_client(tmp_path) as client:
        # Owner "andres" submits a task attributed to companion "limo".
        created = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "private-job-1",
            "task": "Owner private task",
            "companion": "limo",
            "requester": "andres",
        }).json()["result"]["structuredContent"]
        job_id = created["job_id"]

        # 1. Unauthenticated caller cannot submit or read (HTTP 401).
        assert _call_mcp(client, None, "tools/call", {
            "name": "queue_task", "arguments": {"request_id": "x", "task": "y"},
        }).status_code == 401
        assert _call_mcp(client, "bogus-token", "tools/call", {
            "name": "get_task", "arguments": {"job_id": job_id},
        }).status_code == 401
        assert client.post("/queue/worker/claim", json={"version": 1, "limit": 1}).status_code == 401
        assert client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 1},
            headers={"Authorization": "Bearer bogus-worker-token"},
        ).status_code == 401

        # 2. Authenticated MCP caller WITHOUT queue grant cannot submit or read (403).
        for name, args in (
            ("queue_task", {"request_id": "unpriv-1", "task": "should fail"}),
            ("get_task", {"job_id": job_id}),
            ("get_task", {"request_id": "private-job-1"}),
        ):
            res = _call_tool(client, UNPRIVILEGED_MCP_TOKEN, name, args).json()["result"]
            assert res["isError"] is True
            assert res["structuredContent"]["error"]["status"] == 403

        # 3. Different authenticated owner CANNOT read andres's task by job_id or request_id.
        for args in ({"job_id": job_id}, {"request_id": "private-job-1"}):
            res = _call_tool(client, OTHER_OWNER_TOKEN, "get_task", args).json()["result"]
            assert res["isError"] is True
            assert res["structuredContent"]["error"]["status"] == 404

        # Even if other-owner submits with companion="limo" and same request_id,
        # it is isolated to other-owner and other-route; it does not collide with andres.
        other_created = _call_tool(client, OTHER_OWNER_TOKEN, "queue_task", {
            "request_id": "private-job-1",
            "task": "Other owner task",
            "companion": "limo",
            "requester": "andres",
        }).json()["result"]["structuredContent"]
        assert other_created["job_id"] != job_id

        # 4. Worker credential CANNOT call /mcp (HTTP 403).
        assert _call_mcp(client, WORKER_YOGA_TOKEN, "tools/list").status_code == 403
        assert _call_mcp(client, WORKER_YOGA_TOKEN, "tools/call", {
            "name": "get_task", "arguments": {"job_id": job_id},
        }).status_code == 403

        # 5. Owner credentials CANNOT call /queue/worker/* (HTTP 403).
        for owner_tok in (CHAT1_TOKEN, UNPRIVILEGED_MCP_TOKEN):
            for path in ("/queue/worker/claim", "/queue/worker/ack", "/queue/worker/status"):
                r = client.post(
                    path,
                    json={"version": 1, "limit": 1},
                    headers={"Authorization": f"Bearer {owner_tok}"},
                )
                assert r.status_code == 403

        # 6. Worker on route B CANNOT claim or ack route A's job.
        claim_a = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 8},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json()["claims"][0]
        cross_ack = client.post(
            "/queue/worker/ack",
            json={
                "version": 1,
                "bridge_id": claim_a["bridge_id"],
                "lease_id": claim_a["lease_id"],
                "job_id": 99,
            },
            headers={"Authorization": f"Bearer {WORKER_OTHER_TOKEN}"},
        )
        assert cross_ack.status_code == 404


def test_overlapping_worker_and_owner_credentials_rejected_at_startup(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        _make_client(
            tmp_path,
            queue_workers=[QueueWorkerBinding(token=CHAT1_TOKEN, route="yoga-primary")],
        )
    with pytest.raises(ValueError, match="distinct"):
        _make_client(
            tmp_path,
            queue_workers=[
                QueueWorkerBinding(token=WORKER_YOGA_TOKEN, route="route-a"),
                QueueWorkerBinding(token=WORKER_YOGA_TOKEN, route="route-b"),
            ],
        )


def test_settings_from_env_parses_queue_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_QUEUE_DB_PATH", str(tmp_path / "env-relay.sqlite3"))
    monkeypatch.setenv("MCP_QUEUE_DEFAULT_ROUTE", "yoga-env")
    monkeypatch.setenv("MCP_QUEUE_OAUTH_OWNER", "andres")
    monkeypatch.setenv("MCP_QUEUE_OAUTH_ALLOWED_CLIENTS", "c1,c2")
    monkeypatch.setenv("MCP_QUEUE_WORKER_TOKEN", "env-worker-secret")
    monkeypatch.setenv("MCP_QUEUE_WORKER_ID", "yoga-env-poller")
    monkeypatch.setenv(
        "MCP_QUEUE_CALLERS_JSON",
        json.dumps([{"token": "env-caller-secret", "owner": "andres", "route": "yoga-env", "label": "env-chat"}]),
    )
    settings = MCPSettings.from_env()
    assert settings.queue_db_path == str(tmp_path / "env-relay.sqlite3")
    assert settings.queue_default_route == "yoga-env"
    assert settings.queue_oauth_owner == "andres"
    assert settings.queue_oauth_allowed_clients == ["c1", "c2"]
    assert len(settings.queue_callers) == 1
    assert settings.queue_callers[0].owner == "andres"
    assert len(settings.queue_workers) == 1
    assert settings.queue_workers[0].route == "yoga-env"
    assert settings.queue_workers[0].worker_id == "yoga-env-poller"


def test_auth_disabled_fails_closed_for_queue_tools(tmp_path):
    with _make_client(tmp_path, auth_required=False) as client:
        listed = _call_mcp(client, None, "tools/list").json()["result"]["tools"]
        assert not ({t["name"] for t in listed} & {"queue_task", "get_task"})
        res = _call_mcp(client, None, "tools/call", {
            "name": "queue_task",
            "arguments": {"request_id": "anon-1", "task": "blocked"},
        }).json()["result"]
        assert res["isError"] is True
        assert res["structuredContent"]["error"]["status"] == 403


def test_oauth_multi_session_owner_sharing_and_allowlist(tmp_path):
    async def fake_admin_verify(secret: str, totp_code: str) -> bool:
        return secret == "root-secret" and totp_code == "123456"

    settings = _make_settings(
        tmp_path,
        queue_oauth_owner="andres",
        queue_default_route="yoga-primary",
    )
    app = create_mcp_app(bridge=FakeBridge(), settings=settings, admin_verify=fake_admin_verify)
    with TestClient(app) as client:
        _, token_session_1 = _oauth_mint_token(client, "Claude Chat 1")
        client_id_2, token_session_2 = _oauth_mint_token(client, "ChatGPT Chat 2")

        # Chat 1 submits via OAuth.
        submitted = _call_tool(client, token_session_1, "queue_task", {
            "request_id": "oauth-req-1",
            "task": "Cross-client OAuth test",
            "companion": "limo",
        }).json()["result"]
        assert submitted["isError"] is False
        job_id = submitted["structuredContent"]["job_id"]

        # Chat 2 (different OAuth client_id, same owner) reads and retries idempotently.
        retried = _call_tool(client, token_session_2, "queue_task", {
            "request_id": "oauth-req-1",
            "task": "Cross-client OAuth test",
            "companion": "limo",
        }).json()["result"]["structuredContent"]
        assert retried["job_id"] == job_id

        fetched = _call_tool(client, token_session_2, "get_task", {"job_id": job_id}).json()["result"]["structuredContent"]
        assert fetched["job_id"] == job_id

        # OAuth owner token is still rejected on worker routes (403).
        assert client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 1},
            headers={"Authorization": f"Bearer {token_session_1}"},
        ).status_code == 403

        # Restricting queue_oauth_allowed_clients denies non-listed OAuth clients.
        settings.queue_oauth_allowed_clients = [client_id_2]
        denied = _call_tool(client, token_session_1, "get_task", {"job_id": job_id}).json()["result"]
        assert denied["isError"] is True
        assert denied["structuredContent"]["error"]["status"] == 403


def test_worker_http_limits_leases_persistence_and_redacted_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    now = [2_000.0]
    secret_task_text = "TOP-SECRET-TASK-PAYLOAD-XYZ"
    secret_receipt_text = "TOP-SECRET-RECEIPT-BODY-999"

    # First process: submit and test lease expiry + worker error handling.
    with _make_client(tmp_path, clock=lambda: now[0], queue_worker_rate_limit_per_minute=20) as client:
        sub = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "durable-restart-1",
            "task": secret_task_text,
            "companion": "limo",
        }).json()["result"]["structuredContent"]
        job_id = sub["job_id"]

        # Oversized worker body (> 1 MiB) is rejected with 413 before parsing.
        oversized = client.post(
            "/queue/worker/claim",
            content=b"{" + (b" " * MAX_BODY) + b"}",
            headers={
                "Authorization": f"Bearer {WORKER_YOGA_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        assert oversized.status_code == 413

        # Malformed JSON -> 400.
        bad_json = client.post(
            "/queue/worker/claim",
            content=b"{not-json",
            headers={
                "Authorization": f"Bearer {WORKER_YOGA_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        assert bad_json.status_code == 400

        # Expired lease enters delivery_uncertain and rejects late ack (409).
        claim = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 1},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json()["claims"][0]
        now[0] = claim["deadline"]
        read_uncertain = _call_tool(client, CHAT2_TOKEN, "get_task", {"job_id": job_id}).json()["result"]["structuredContent"]
        assert read_uncertain["delivery_state"] == "delivery_uncertain"
        late_ack = client.post(
            "/queue/worker/ack",
            json={
                "version": 1,
                "bridge_id": claim["bridge_id"],
                "lease_id": claim["lease_id"],
                "job_id": 1,
            },
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        )
        assert late_ack.status_code == 409

        # Submit a second task and complete it to check receipt redaction in logs.
        sub2 = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "durable-restart-2",
            "task": secret_task_text,
            "companion": "siro",
        }).json()["result"]["structuredContent"]
        claim2 = client.post(
            "/queue/worker/claim",
            json={"version": 1, "limit": 1},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).json()["claims"][0]
        assert client.post(
            "/queue/worker/ack",
            json={"version": 1, "bridge_id": claim2["bridge_id"], "lease_id": claim2["lease_id"], "job_id": 2},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).status_code == 200
        assert client.post(
            "/queue/worker/status",
            json={
                "version": 1,
                "bridge_id": claim2["bridge_id"],
                "job_id": 2,
                "snapshot": {
                    "id": 2, "priority": 0, "state": "done", "attempts": 1,
                    "result": {"receipt": secret_receipt_text}, "finished": now[0],
                },
            },
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).status_code == 200

    # Second process (simulated restart): same SQLite DB retains jobs and deduplication!
    with _make_client(tmp_path, clock=lambda: now[0]) as restarted:
        retry = _call_tool(restarted, CHAT2_TOKEN, "queue_task", {
            "request_id": "durable-restart-2",
            "task": secret_task_text,
            "companion": "siro",
        }).json()["result"]["structuredContent"]
        assert retry["job_id"] == sub2["job_id"]
        assert retry["state"] == "done"
        assert retry["receipt"] == {"receipt": secret_receipt_text}

    # Verify logs contain NO credentials, task payload, or receipt content.
    assert CHAT1_TOKEN not in caplog.text
    assert CHAT2_TOKEN not in caplog.text
    assert WORKER_YOGA_TOKEN not in caplog.text
    assert secret_task_text not in caplog.text
    assert secret_receipt_text not in caplog.text


def test_worker_rate_limit_enforced(tmp_path):
    with _make_client(tmp_path, queue_worker_rate_limit_per_minute=2) as client:
        headers = {"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"}
        assert client.post("/queue/worker/claim", json={"version": 1, "limit": 1}, headers=headers).status_code == 200
        assert client.post("/queue/worker/claim", json={"version": 1, "limit": 1}, headers=headers).status_code == 200
        limited = client.post("/queue/worker/claim", json={"version": 1, "limit": 1}, headers=headers)
        assert limited.status_code == 429


def test_worker_heartbeat_records_liveness_and_enforces_auth(tmp_path):
    now = [3_000.0]
    with _make_client(tmp_path, clock=lambda: now[0]) as client:
        sub = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "hb-check-1",
            "task": "Verify heartbeat visibility",
        }).json()["result"]["structuredContent"]
        assert sub["worker_heartbeat_at"] is None

        # Unauthenticated and owner credentials rejected on heartbeat.
        assert client.post("/queue/worker/heartbeat", json={"version": 1, "worker_state": "running"}).status_code == 401
        assert client.post(
            "/queue/worker/heartbeat",
            json={"version": 1, "worker_state": "running"},
            headers={"Authorization": f"Bearer {CHAT1_TOKEN}"},
        ).status_code == 403

        # Invalid worker_state rejected with 400.
        assert client.post(
            "/queue/worker/heartbeat",
            json={"version": 1, "worker_state": "bogus"},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        ).status_code == 400

        # Valid heartbeat from Yoga worker updates observed_at surfaced in get_task.
        now[0] = 3_045.5
        hb = client.post(
            "/queue/worker/heartbeat",
            json={"version": 1, "worker_state": "running", "queued_local": 1, "running_local": 0},
            headers={"Authorization": f"Bearer {WORKER_YOGA_TOKEN}"},
        )
        assert hb.status_code == 200
        assert hb.json() == {"version": 1, "ok": True, "observed_at": 3_045.5}

        fetched = _call_tool(client, CHAT2_TOKEN, "get_task", {"job_id": sub["job_id"]}).json()["result"]["structuredContent"]
        assert fetched["worker_heartbeat_at"] == 3_045.5


def test_jobqueue_relay_client_end_to_end_round_trip(tmp_path):
    """Exercise actual jobqueue.relay_client.RelayClient + local queue DB
    against the Companions MCP FastAPI app when /home/agent/work is on sys.path."""
    relay_client = pytest.importorskip("jobqueue.relay_client")
    local_worker = pytest.importorskip("hilo.worker")

    with _make_client(tmp_path) as client:
        # 1. Companion in Chat 1 submits a task.
        first = _call_tool(client, CHAT1_TOKEN, "queue_task", {
            "request_id": "yoga-e2e-001",
            "task": "Verify queue relay round trip and reply DONE Queue round-trip ok",
            "backend": "codex",
            "priority": 10,
            "effort": "medium",
            "requester": "siro",
            "executor": "limo",
            "task_kind": "review",
            "delegation_mode": "queue_only",
        }).json()["result"]["structuredContent"]
        stable_job_id = first["job_id"]

        # 2. Retry the exact same request from Chat 2 -> identical job ID, no duplicate.
        retry = _call_tool(client, CHAT2_TOKEN, "queue_task", {
            "request_id": "yoga-e2e-001",
            "task": "Verify queue relay round trip and reply DONE Queue round-trip ok",
            "backend": "codex",
            "priority": 10,
            "effort": "medium",
            "requester": "siro",
            "executor": "limo",
            "task_kind": "review",
            "delegation_mode": "queue_only",
        }).json()["result"]["structuredContent"]
        assert retry["job_id"] == stable_job_id

        # 3. Unauthorized caller cannot submit or read.
        unauth = _call_tool(client, UNPRIVILEGED_MCP_TOKEN, "get_task", {"job_id": stable_job_id}).json()["result"]
        assert unauth["isError"] is True
        assert unauth["structuredContent"]["error"]["status"] == 403

        # 4. Yoga's real RelayClient pulls outbound using its separate worker credential.
        def asgi_transport(host, port, path, headers, body, timeout):
            assert host == "companions.local"
            assert port == 443
            assert path.startswith("/queue/worker/")
            resp = client.post(path, content=body, headers=headers)
            if resp.status_code != 200:
                raise relay_client.RelayError(f"Relay returned HTTP {resp.status_code}")
            return resp.content

        with tempfile.TemporaryDirectory() as yoga_state_dir:
            with patch.object(local_worker, "STATE", Path(yoga_state_dir)):
                poller = relay_client.RelayClient(
                    "https://companions.local",
                    WORKER_YOGA_TOKEN,
                    transport=asgi_transport,
                )
                hb = poller.heartbeat("idle", queued_local=0, running_local=0)
                assert hb["ok"] is True

                poll1 = poller.poll_once()
                assert poll1 == {"delivered": 1, "published": 0, "rejected": 0}

                # Confirm Yoga's local DB has exactly ONE job and ONE intake mapping.
                with closing(local_worker.connect()) as local_db:
                    jobs = local_db.execute("SELECT id, state FROM jobs").fetchall()
                    intakes = local_db.execute("SELECT request_id, job_id FROM intake_requests").fetchall()
                    prompt = local_db.execute(
                        "SELECT backend, effort, companion, requester, executor, task_kind, delegation_mode "
                        "FROM prompts WHERE job_id = 1"
                    ).fetchone()
                    assert [tuple(j) for j in jobs] == [(1, "queued")]
                    assert len(intakes) == 1
                    assert tuple(prompt) == ("codex", "medium", "limo", "siro", "limo", "review", "queue_only")

                    # Simulate Yoga local worker completing the single execution and recording its receipt.
                    local_db.execute(
                        "UPDATE jobs SET state = 'done', attempts = 1, result = ?, finished = ? WHERE id = 1",
                        (json.dumps({"receipt": "DONE Queue round-trip ok", "worker": "yoga"}), 1_795_290_500.0),
                    )
                    local_db.commit()

                # Poll again to push the terminal snapshot back to Companions MCP.
                poll2 = poller.poll_once()
                assert poll2 == {"delivered": 0, "published": 1, "rejected": 0}

                # Further poll sees nothing pending and nothing non-terminal.
                poll3 = poller.poll_once()
                assert poll3 == {"delivered": 0, "published": 0, "rejected": 0}

        # 5. Second authorized chat retrieves the terminal receipt by job_id and request_id.
        receipt_by_job = _call_tool(client, CHAT2_TOKEN, "get_task", {"job_id": stable_job_id}).json()["result"]["structuredContent"]
        receipt_by_req = _call_tool(client, CHAT2_TOKEN, "get_task", {"request_id": "yoga-e2e-001"}).json()["result"]["structuredContent"]
        assert receipt_by_job == receipt_by_req
        assert receipt_by_job["job_id"] == stable_job_id
        assert receipt_by_job["delivery_state"] == "delivered"
        assert receipt_by_job["state"] == "done"
        assert receipt_by_job["worker_job_id"] == 1
        assert receipt_by_job["worker_heartbeat_at"] is not None
        assert receipt_by_job["receipt"] == {"receipt": "DONE Queue round-trip ok", "worker": "yoga"}
        assert receipt_by_job["companion"] is None
        assert receipt_by_job["requester"] == "siro"
        assert receipt_by_job["executor"] == "limo"

        # Confirm relay SQLite has exactly 1 row total.
        with closing(sqlite3.connect(tmp_path / "queue-relay.sqlite3")) as relay_db:
            count = relay_db.execute("SELECT COUNT(*) FROM relay_requests").fetchone()[0]
            assert count == 1
