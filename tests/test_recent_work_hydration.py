"""Tests for conversation-only recent_work hydration, mode separation, time window, and missing dreams."""

import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from profile_os.api import create_app
from profile_os.bridge import ToolBridge
from profile_os.mcp_server import CloseoutCodeState, MCPToolRunner
from profile_os.storage import Store
from profile_os.tool_schemas import START_SESSION


@pytest.fixture
def env(tmp_path):
    identity = tmp_path / "identity.md"
    identity.write_text("OWNER_PRIVATE_CANARY")
    app = create_app(data_dir=str(tmp_path / "data"), identity_file=str(identity), do_seed=False)
    store = app.state.store
    store.create_profile("limo", "Limo", "I am Limo.", "Build repairable software.")
    store.update_prompts("limo", voice="Dry thunder.", lane="Coding and infrastructure.")
    store.write_file("limo", "SOUL.md", "# Limo garden\n\nGARDEN_ONLY_CANARY\n")
    store.remember("limo", {"kind": "note", "content": "MEMORY_CANARY", "tags": []})
    store.closeout("limo", "Chat closeout canary", "dry", "Andres: check\nLimo: ok")
    with TestClient(app) as client:
        bridge = ToolBridge(client=client)
        runner = MCPToolRunner(
            bridge,
            closeout_signing_key="test",
            closeout_code_state=CloseoutCodeState(str(tmp_path / "closeout.db")),
        )
        yield app, client, bridge, runner


def _validate(packet):
    Draft202012Validator(START_SESSION).validate(packet)


def test_missing_dreams_hydrates_empty_recent_work_in_conversation_only(env):
    app, client, bridge, runner = env
    bare = runner.call("summon_companion", {"profile_id": "limo"})
    _validate(bare)
    assert bare["recent_work"] == ""

    conv = runner.call("summon_companion", {"profile_id": "limo", "mode": "conversation"})
    _validate(conv)
    assert conv["recent_work"] == ""

    for mode in ("workbench", "editor", "actor", "forum"):
        args = {"profile_id": "limo", "mode": mode}
        if mode == "workbench":
            args["task"] = {"purpose": "Ship feature", "brief": "Use task intent"}
        packet = runner.call("summon_companion", args)
        _validate(packet)
        assert "recent_work" not in packet


def test_mode_separation_conversation_and_secondary_get_recent_work_while_task_modes_do_not(env, monkeypatch):
    app, client, bridge, runner = env
    store = app.state.store
    text = "I have been working on: tightening review wakes and separating task closeouts."
    store.set_recent_work("limo", text, window_days=7)

    bare = runner.call("summon_companion", {"profile_id": "limo"})
    _validate(bare)
    assert bare["recent_work"] == text

    conv = runner.call("summon_companion", {"profile_id": "limo", "mode": "conversation"})
    _validate(conv)
    assert conv["recent_work"] == text

    secondary = runner.call(
        "summon_companion",
        {
            "profile_id": "limo",
            "mode": "secondary",
            "session": {"id": "chat-2", "available_sections": ["system_contracts"]},
        },
    )
    _validate(secondary)
    assert secondary["invocation"]["mode"] == "conversation"
    assert secondary["hydration"]["effective"] == "delta"
    assert secondary["recent_work"] == text

    forum = runner.call("summon_companion", {"profile_id": "limo", "mode": "forum"})
    _validate(forum)
    assert "recent_work" not in forum

    # Focused / non-conversation invocation modes must not even read recent_work.
    def forbidden(*args, **kwargs):
        raise AssertionError("recent_work must not be read outside conversation mode")

    monkeypatch.setattr(store, "read_recent_work", forbidden)

    workbench = runner.call(
        "summon_companion",
        {
            "profile_id": "limo",
            "mode": "workbench",
            "task": {
                "purpose": "Resume job-99",
                "brief": "Use exact checkpoint and wake results",
                "environment": {"kind": "other", "task_ref": "job-99", "authorization_scope": "edit"},
            },
        },
    )
    _validate(workbench)
    assert "recent_work" not in workbench
    assert workbench["invocation"]["task"]["purpose"] == "Resume job-99"
    assert workbench["invocation"]["task"]["brief"] == "Use exact checkpoint and wake results"

    for mode in ("editor", "actor"):
        packet = runner.call("summon_companion", {"profile_id": "limo", "mode": mode})
        _validate(packet)
        assert "recent_work" not in packet

    forum_full = runner.call(
        "summon_companion",
        {"profile_id": "limo", "mode": "forum", "hydration": "full"},
    )
    _validate(forum_full)
    assert "recent_work" not in forum_full


def test_recent_work_time_window_expiry_and_isolation_from_garden_and_closeouts(env):
    app, client, bridge, runner = env
    store = app.state.store
    initial_soul = store.read_soul("limo")
    initial_boot = store.boot("limo")
    initial_log = store.recent_session_log("limo")
    initial_exchanges = store.recent_exchanges("limo")

    now = 1_800_000_000.0
    store.set_recent_work(
        "limo",
        "I have been working on: hardening queue routing.",
        window_days=7,
        window_start=now - 7 * 86400,
        window_end=now,
        updated_at=now,
    )
    assert store.read_recent_work("limo", now=now + 6 * 86400) == (
        "I have been working on: hardening queue routing."
    )
    # After 7 days pass without a fresh dream reflection, stale recent_work expires.
    assert store.read_recent_work("limo", now=now + 7 * 86400 + 1) == ""

    # Garden and conversation closeout lifecycle remain untouched.
    assert store.read_soul("limo") == initial_soul
    assert store.boot("limo")["compact_state"] == initial_boot["compact_state"]
    assert store.recent_session_log("limo") == initial_log
    assert store.recent_exchanges("limo") == initial_exchanges

    # Bounds enforced on storage and HTTP schema.
    over_limit = "I have been working on: " + ("x" * (Store.MAX_RECENT_WORK_CHARS + 1))
    assert client.put("/profiles/limo/recent-work", json={"content": over_limit}).status_code == 422


def test_recent_work_auth_and_dream_to_conversation_integration(tmp_path):
    work_root = Path(__file__).resolve().parents[2]
    taskqueue_root = next(
        (candidate for candidate in (work_root / "taskqueue", work_root / "work" / "taskqueue")
         if candidate.is_dir()),
        None,
    )
    if taskqueue_root is not None and str(taskqueue_root.parent) not in sys.path:
        sys.path.insert(0, str(taskqueue_root.parent))
    import sqlite3
    from taskqueue import dreams

    app = create_app(data_dir=str(tmp_path / "auth_data"), auth_enabled=True, do_seed=False)
    app.state.store.create_profile("limo", "Limo", "Gecko.", "Ship code.")
    app.state.store.create_profile("tara", "Tara", "Helper.", "Coordinate.")
    p = app.state.access.create_principal("bridge", "Bridge")
    cred_id = app.state.access.create_credential(p["id"], "test", "bridge-secret")
    assert cred_id
    app.state.access.grant(p["id"], "remember", profile_id="limo")
    app.state.access.grant(p["id"], "boot", profile_id="limo")

    with TestClient(app) as client:
        # Unauthenticated and ungranted requests rejected.
        assert client.put("/profiles/limo/recent-work", json={"content": "x"}).status_code == 401
        assert client.put(
            "/profiles/tara/recent-work",
            headers={"Authorization": "Bearer bridge-secret"},
            json={"content": "x"},
        ).status_code == 403

        bridge = ToolBridge(bearer="bridge-secret", client=client)
        runner = MCPToolRunner(
            bridge,
            closeout_signing_key="test",
            closeout_code_state=CloseoutCodeState(str(tmp_path / "closeout.db")),
        )

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT, priority INTEGER, due REAL, interval INTEGER, state TEXT, attempts INTEGER, result TEXT, finished REAL, started REAL)")
        dreams.ensure_dream_tables(db)
        now = 1_800_000_000.0
        dreams.configure_dream_schedule(db, "limo", next_due_at=now, now=now - 10)
        res = dreams.record_dream_reflection(
            db,
            job_id=None,
            profile_id="limo",
            window_start=now - 86400,
            window_end=now,
            closeouts_available=True,
            observed_closeout_job_ids=[61, 63],
            reflection={
                "work_summary": {
                    "status": "summarized",
                    "cited_job_ids": [61, 63],
                    "completed_tasks": ["Write software skill", "Add review wakes"],
                    "summary": "Delivered Queue software workflow for pr:org/repo#63@deadbeef1234 and review wakes.",
                },
                "garden_reflection": {
                    "soul_changed": False,
                    "reflection": "GARDEN_SECRET_INTEREST stays separate.",
                },
            },
            now=now,
            recent_work_writer=bridge.set_recent_work,
        )
        assert res["recent_work_sync"]["sync_status"] == "synced"
        # Hydrate via real store read_recent_work anchored at `now`.
        conv = app.state.store.read_recent_work("limo", now=now + 60)
        assert conv.startswith("I have been working on: ")
        assert "Queue software workflow" in conv
        assert "#63" not in conv
        assert "deadbeef" not in conv
        assert "GARDEN_SECRET_INTEREST" not in conv

        # Also verify live MCP summon_companion mode separation with fresh timestamp.
        bridge.set_recent_work("limo", conv, window_days=7)
        conv_packet = runner.call("summon_companion", {"profile_id": "limo", "mode": "conversation"})
        _validate(conv_packet)
        assert conv_packet["recent_work"] == conv

        task_packet = runner.call(
            "summon_companion",
            {
                "profile_id": "limo",
                "mode": "workbench",
                "task": {"purpose": "Verify task mode", "brief": "Task intent only"},
            },
        )
        _validate(task_packet)
        assert "recent_work" not in task_packet
        assert task_packet["invocation"]["task"]["purpose"] == "Verify task mode"
        db.close()
