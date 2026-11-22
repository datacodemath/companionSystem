"""Cross-repository protocol check for task-system's real relay client."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from profile_os.mcp_server import (
    MCPSettings,
    QueueCallerBinding,
    QueueWorkerBinding,
    create_mcp_app,
)
from test_mcp_server import FakeBridge, ORIGIN, PUBLIC_BASE, _rpc


TASK_SYSTEM = Path("/home/agent/tasks")
OWNER_TOKEN = "task-system-integration-owner"
WORKER_TOKEN = "task-system-integration-worker"


def _load_task_system_relay():
    """Load task-system modules without leaving its local queue.py shadowing stdlib."""
    if not (TASK_SYSTEM / "relay_worker.py").is_file():
        pytest.skip("task-system checkout is unavailable")
    sys.path.insert(0, str(TASK_SYSTEM))
    try:
        for module_name in ("io_models", "task_executor"):
            if module_name not in sys.modules:
                spec = importlib.util.spec_from_file_location(
                    module_name, TASK_SYSTEM / f"{module_name}.py"
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                assert spec.loader is not None
                spec.loader.exec_module(module)

        prior_queue = sys.modules.get("queue")
        queue_spec = importlib.util.spec_from_file_location("queue", TASK_SYSTEM / "queue.py")
        task_queue = importlib.util.module_from_spec(queue_spec)
        sys.modules["queue"] = task_queue
        assert queue_spec.loader is not None
        queue_spec.loader.exec_module(task_queue)
        try:
            relay_spec = importlib.util.spec_from_file_location(
                "task_system_relay_worker", TASK_SYSTEM / "relay_worker.py"
            )
            relay = importlib.util.module_from_spec(relay_spec)
            sys.modules[relay_spec.name] = relay
            assert relay_spec.loader is not None
            relay_spec.loader.exec_module(relay)
        finally:
            if prior_queue is None:
                sys.modules.pop("queue", None)
            else:
                sys.modules["queue"] = prior_queue
        return relay, task_queue
    finally:
        sys.path.remove(str(TASK_SYSTEM))


def test_task_system_relay_round_trip_against_companions_app(tmp_path):
    settings = MCPSettings(
        auth_required=True,
        connector_tokens=[],
        allowed_origins=[ORIGIN],
        public_base_url=PUBLIC_BASE,
        oauth_issuer=PUBLIC_BASE,
        oauth_signing_key="task-system-integration-signing-key",
        closeout_code_state_file=str(tmp_path / "closeout.sqlite3"),
        queue_db_path=str(tmp_path / "companions.sqlite3"),
        queue_default_route="yoga-primary",
        queue_callers=[QueueCallerBinding(
            token=OWNER_TOKEN, owner="andres", route="yoga-primary", label="integration",
        )],
        queue_workers=[QueueWorkerBinding(
            token=WORKER_TOKEN, route="yoga-primary", worker_id="task-system",
        )],
    )
    app = create_mcp_app(bridge=FakeBridge(), settings=settings)
    with TestClient(app) as http:
        relay_module, task_queue = _load_task_system_relay()
        submit = http.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "queue_task", "arguments": {
                "request_id": "task-system-round-trip-1",
                "task": "Return a verified task-system relay receipt",
                "backend": "codex",
                "requester": "siro",
                "executor": "limo",
            }}, 11),
            headers={"Authorization": f"Bearer {OWNER_TOKEN}", "Origin": ORIGIN},
        ).json()["result"]["structuredContent"]

        def asgi_transport(host, port, path, headers, body, timeout):
            assert (host, port) == ("companions.local", 443)
            response = http.post(path, content=body, headers=headers)
            assert response.status_code == 200, response.text
            return response.content

        with tempfile.TemporaryDirectory() as local_state:
            db = Path(local_state) / "task-system.sqlite3"
            client = relay_module.RelayClient(
                "https://companions.local", WORKER_TOKEN, db,
                repository=local_state, companion_id="fallback", transport=asgi_transport,
            )
            assert client.poll_once() == {"delivered": 1, "published": 0, "rejected": 0}
            # The server-issued bridge ID is durable in the local binding table.
            binding_db = task_queue._connect(task_queue._database_path(db))
            try:
                bridge_id, relay_job_id = binding_db.execute(
                    "SELECT bridge_id, relay_job_id FROM relay_bindings"
                ).fetchone()
            finally:
                binding_db.close()
            assert relay_job_id == 1
            task = task_queue.get_task(db, request_id=bridge_id)
            assert task["state"] == "queued"

            def execute(task_input, companion, result_path):
                assert companion == "limo"
                from io_models import Evidence, TaskOutput, TaskStatus
                return TaskOutput(
                    task_id=task_input.task_id,
                    status=TaskStatus.DONE,
                    summary="Relay integration verified",
                    evidence=[Evidence(description="Round trip", reference="test", verified=True)],
                )

            assert task_queue.work(db, executor=execute) == (1, 0)
            assert client.poll_once() == {"delivered": 0, "published": 1, "rejected": 0}

        receipt = http.post(
            "/mcp",
            json=_rpc("tools/call", {"name": "get_task", "arguments": {
                "job_id": submit["job_id"],
            }}, 12),
            headers={"Authorization": f"Bearer {OWNER_TOKEN}", "Origin": ORIGIN},
        ).json()["result"]["structuredContent"]
        assert receipt["delivery_state"] == "delivered"
        assert receipt["state"] == "done"
        assert receipt["worker_job_id"] == 1
        assert receipt["receipt"]["summary"] == "Relay integration verified"
