"""Tests separating Queue task closeouts from companion conversations and memories."""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.bridge import ToolBridge
from profile_os.mcp_server import (
    CloseoutCodeState,
    MCPSettings,
    QueueCallerBinding,
    QueueWorkerBinding,
    create_mcp_app,
)
from profile_os.storage import Store


def test_task_participation_memory_upsert_and_conversation_independence(tmp_path):
    store = Store(tmp_path / "data")
    store.create_profile("limo", "Limo", "Thunder gecko.", "Build software.")
    store.closeout("limo", "Conversation facts stay separate", "dry thunder", "Andres: check\nLimo: ok")
    initial_state = store.boot("limo")["compact_state"]
    initial_log = store.recent_session_log("limo")
    initial_exchanges = store.recent_exchanges("limo")

    first = store.upsert_task_participation_memory(
        "limo",
        "job-64",
        content="Participated in job-64 (Separate task closeouts) as executor; outcome: blocked.",
        tags=["task_outcome:blocked"],
    )
    second = store.upsert_task_participation_memory(
        "limo",
        "job-64",
        content="Participated in job-64 (Separate task closeouts) as executor; outcome: done.",
        tags=["task_outcome:done"],
    )
    assert first["id"] == second["id"]
    memories = store.all_memories("limo")
    assert len(memories) == 1
    assert "outcome: done" in memories[0]["content"]
    assert "task_participation:job-64" in memories[0]["tags"]

    # Conversation lifecycle artifacts remain completely independent.
    assert store.boot("limo")["compact_state"] == initial_state
    assert store.recent_session_log("limo") == initial_log
    assert store.recent_exchanges("limo") == initial_exchanges
    assert len(store.recent_closeouts("limo", limit=10)) == 1


def test_relay_status_writes_one_sanitized_memory_per_companion_without_history(tmp_path):
    api_app = create_app(data_dir=str(tmp_path / "profile_data"), auth_enabled=False, do_seed=False)
    store = api_app.state.store
    store.create_profile("tara", "Tara", "Helper.", "Coordinate work.")
    store.create_profile("limo", "Limo", "Gecko.", "Ship code.")
    store.closeout("tara", "Tara chat closeout", "calm", "Andres: hello\nTara: hi")
    store.closeout("limo", "Limo chat closeout", "charged", "Andres: ping\nLimo: zap")

    with TestClient(api_app) as api_client:
        bridge = ToolBridge(client=api_client)
        settings = MCPSettings(
            auth_required=True,
            connector_tokens=[],
            allowed_origins=["https://claude.ai"],
            public_base_url="https://profiles.example",
            oauth_issuer="https://profiles.example",
            oauth_signing_key="test-key",
            closeout_code_state_file=str(tmp_path / "closeout-codes.db"),
            queue_db_path=str(tmp_path / "relay.db"),
            queue_default_route="yoga-primary",
            queue_callers=[
                QueueCallerBinding(
                    token="owner-token",
                    owner="andres",
                    route="yoga-primary",
                    label="chat-1",
                )
            ],
            queue_workers=[
                QueueWorkerBinding(
                    token="worker-token",
                    route="yoga-primary",
                    worker_id="yoga-worker",
                )
            ],
        )
        mcp_app = create_mcp_app(bridge=bridge, settings=settings)
        with TestClient(mcp_app) as mcp_client:
            owner_headers = {
                "Authorization": "Bearer owner-token",
                "Origin": "https://claude.ai",
                "Accept": "application/json",
            }
            worker_headers = {"Authorization": "Bearer worker-token"}
            queued = mcp_client.post(
                "/mcp",
                headers=owner_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "queue_task",
                        "arguments": {
                            "request_id": "req-closeout-mem-1",
                            "task": "Fix closeout bridge for org/repo#88 at deadbeef1234 verdict: changes_requested",
                            "backend": "codex",
                            "requester": "tara",
                            "executor": "limo",
                            "task_kind": "implementation",
                        },
                    },
                },
            ).json()["result"]["structuredContent"]

            claims = mcp_client.post(
                "/queue/worker/claim",
                headers=worker_headers,
                json={"version": 1, "limit": 4},
            ).json()["claims"]
            assert len(claims) == 1
            bridge_id = claims[0]["bridge_id"]
            lease_id = claims[0]["lease_id"]

            ack = mcp_client.post(
                "/queue/worker/ack",
                headers=worker_headers,
                json={"version": 1, "bridge_id": bridge_id, "lease_id": lease_id, "job_id": 64},
            )
            assert ack.status_code == 200

            # Initial terminal status (done) plus idempotent duplicate status push.
            snapshot = {
                "id": 64,
                "priority": 0,
                "state": "done",
                "attempts": 1,
                "finished": 1795334000.0,
                "result": {
                    "companion": "limo",
                    "outcome_kind": "done",
                    "result_contract": {
                        "version": 1,
                        "job_id": 64,
                        "outcome": "done",
                        "companion": "limo",
                        "loaded_via": "Companions MCP",
                        "artifacts": ["pr:org/repo#88@deadbeef1234"],
                        "checks": ["pytest ok"],
                        "blocker": None,
                        "partial_changes": "None",
                        "deployment": "Not deployed",
                    },
                },
            }
            for _ in range(2):
                resp = mcp_client.post(
                    "/queue/worker/status",
                    headers=worker_headers,
                    json={"version": 1, "bridge_id": bridge_id, "job_id": 64, "snapshot": snapshot},
                )
                assert resp.status_code == 200

    for pid in ("tara", "limo"):
        mems = store.all_memories(pid)
        assert len(mems) == 1
        content = mems[0]["content"]
        assert queued["job_id"] in content
        assert "outcome: done" in content
        assert "#88" not in content
        assert "deadbeef" not in content
        assert "changes_requested" not in content
        assert "verdict" not in content
    assert store.boot("tara")["compact_state"].startswith("## Facts\nTara chat closeout")
    assert store.boot("limo")["compact_state"].startswith("## Facts\nLimo chat closeout")


def test_task_memories_endpoint_auth_and_taskqueue_sync_integration(tmp_path, monkeypatch):
    import sys
    from contextlib import closing
    work_root = Path(__file__).resolve().parents[2]
    taskqueue_root = next(
        (candidate for candidate in (work_root / "taskqueue", work_root / "work" / "taskqueue")
         if candidate.is_dir()),
        None,
    )
    if taskqueue_root is not None and str(taskqueue_root.parent) not in sys.path:
        sys.path.insert(0, str(taskqueue_root.parent))
    from taskqueue import closeouts as tq_closeouts, worker as tq_worker

    app = create_app(data_dir=str(tmp_path / "auth_data"), auth_enabled=True, do_seed=False)
    app.state.store.create_profile("limo", "Limo", "Gecko.", "Ship code.")
    app.state.store.create_profile("tara", "Tara", "Helper.", "Coordinate.")
    principal = app.state.access.create_principal("bridge", "Bridge")
    token = "test-bridge-secret"
    app.state.access.create_credential(principal["id"], "test", token)
    app.state.access.grant(principal["id"], "remember", profile_id="limo")

    with TestClient(app) as client:
        # Unauthenticated rejected
        assert client.post("/profiles/limo/task-memories", json={"task_key": "job-1", "content": "x"}).status_code == 401
        # Missing grant for tara rejected
        assert client.post(
            "/profiles/tara/task-memories",
            headers={"Authorization": f"Bearer {token}"},
            json={"task_key": "job-1", "content": "x"},
        ).status_code == 403
        # Authorized upsert succeeds and updates in place
        r1 = client.post(
            "/profiles/limo/task-memories",
            headers={"Authorization": f"Bearer {token}"},
            json={"task_key": "job-64", "content": "Participated in job-64 as executor; outcome: blocked."},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/profiles/limo/task-memories",
            headers={"Authorization": f"Bearer {token}"},
            json={"task_key": "job-64", "content": "Participated in job-64 as executor; outcome: done."},
        )
        assert r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]
        assert len(app.state.store.all_memories("limo")) == 1

    # End-to-end taskqueue -> Store sync keeping conversation closeout untouched
    monkeypatch.setattr(tq_worker, "STATE", tmp_path / "tq_state")
    app.state.store.closeout("limo", "Limo chat", "dry", "A: hi\nL: ok")
    with closing(tq_worker.connect()) as db:
        with db:
            jid = tq_worker.submit_job(
                db, "agy", "Ship closeout separation", requester="tara", executor="limo",
                handoff={"goal": "Separate task closeouts for org/repo#64 @abcdef12"},
            )
        contract = {
            "version": 1, "job_id": jid, "outcome": "done", "companion": "limo",
            "loaded_via": "Companions MCP", "artifacts": ["taskqueue/closeouts.py"],
            "checks": ["pytest ok"], "blocker": None, "partial_changes": "None", "deployment": "Not deployed",
        }
        tq_worker.record_manual_receipt(db, jid, "done", tq_worker.contract_receipt(contract), result_contract=contract)
        db.commit()
        synced = tq_closeouts.sync_task_participation_memories(
            db,
            jid,
            memory_writer=lambda profile_id, task_key, kind, content, tags: (
                app.state.store.upsert_task_participation_memory(
                    profile_id, task_key, content=content, kind=kind, tags=tags
                )
            ),
        )
        assert len(synced) == 2
        assert all(item["sync_status"] == "synced" for item in synced)
    assert len(app.state.store.all_memories("limo")) == 2  # job-64 + job-<jid>
    assert len(app.state.store.all_memories("tara")) == 1
    assert app.state.store.boot("limo")["compact_state"].startswith("## Facts\nLimo chat")
