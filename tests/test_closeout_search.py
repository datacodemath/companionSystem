"""Historical recall must be explicit, profile-scoped, and hydration-neutral."""
import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from profile_os.api import create_app
from profile_os.bridge import ToolBridge
from profile_os.mcp_server import MCPToolRunner, CloseoutCodeState
from profile_os.tool_schemas import CONTEXT_RESULT


@pytest.fixture
def env(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=False)
    with TestClient(app) as client:
        yield app.state.store, client, tmp_path


def archive(store, cid, profile="tara", at=1, **fields):
    values = {name: fields.get(name, "") for name in
              ("facts", "texture", "exchange", "notes", "new_state")}
    with store.db:
        store.db.execute(
            "INSERT INTO closeouts "
            "(id,profile_id,created_at,facts,texture,exchange,notes,new_state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, profile, at, *(values[name] for name in values)),
        )


@pytest.mark.parametrize("field", ["facts", "texture", "exchange", "notes", "new_state"])
def test_old_fields_retrieved_beyond_recent_hydration(env, field):
    store, client, _ = env
    archive(store, "founding", **{field: "ORIGIN-CANARY: March founding"})
    for i in range(12):
        archive(store, f"later-{i}", at=100 + i, facts="ordinary later session")
    assert all("ORIGIN-CANARY" not in str(item) for item in store.recent_session_log("tara"))
    response = client.get("/profiles/tara/context/search", params={"q": "origin-canary"})
    assert response.status_code == 200
    hits = response.json()
    assert len(hits) == 1
    hit = hits[0]
    Draft202012Validator(CONTEXT_RESULT).validate(hit)
    assert hit["source_type"] == "closeout" and hit["source_name"] == "closeouts"
    assert hit["item"]["id"] == "founding"
    assert hit["item"]["profile_id"] == "tara"
    assert hit["item"]["created_at"] == 1
    assert hit["item"][field].startswith("ORIGIN-CANARY")


def test_all_sources_sort_limit_literal_search_and_no_other_profiles(env):
    store, client, _ = env
    archive(store, "old", facts="shared needle", at=1)
    archive(store, "new", facts="shared needle", new_state="shared needle", at=5)
    archive(store, "secret", profile="sidra", facts="shared needle", at=100)
    store.remember("tara", {"kind": "note", "content": "shared needle", "tags": []})
    hits = client.get("/profiles/tara/context/search", params={"q": "shared needle", "limit": 2}).json()
    assert [h["source_type"] for h in hits] == ["memory", "closeout"]
    assert hits[1]["item"]["id"] == "new"
    assert len(store.search_closeouts("tara", "shared needle")) == 2
    archive(store, "literal", facts=r"100%_safe\path")
    assert [r["id"] for r in store.search_closeouts("tara", "%_safe\\")] == ["literal"]
    assert store.search_closeouts("tara", "' OR 1=1 --") == []


def test_unmigrated_and_migrated_legacy_history_without_writes(env):
    store, client, _ = env
    dyn = client.app.state.dynstores
    schema = {"fields": {"date": {"type": "string"}, "summary": {"type": "string"}}}
    dyn.propose("tara", "old_sessions", "Legacy continuity", "tara", schema)
    dyn.approve("tara", "old_sessions")
    record = dyn.add_record("tara", "old_sessions", {
        "date": "2025-01-01", "summary": "legacy archive landmark",
    })
    # Simulate a store created before session logs became read-only.
    with store.db:
        store.db.execute("UPDATE dynamic_stores SET name='tara_session_log' WHERE profile_id='tara' AND name='old_sessions'")
        store.db.execute("UPDATE dynamic_records SET store_name='tara_session_log' WHERE id=?", (record["id"],))
    hits = client.get("/profiles/tara/context/search", params={"q": "archive landmark"}).json()
    assert len(hits) == 1 and hits[0]["source_type"] == "profile_store"
    assert dyn.get_record("tara", "tara_session_log", record["id"])
    # The existing ordinary closeout absorbs legacy rows, not the search.
    store.closeout("tara", "current fact", "current texture", "User: done")
    hits = client.get("/profiles/tara/context/search", params={"q": "archive landmark"}).json()
    assert len(hits) == 1 and hits[0]["source_type"] == "closeout"
    assert hits[0]["item"]["id"] == "legacy-session-" + record["id"]


def test_search_is_read_only_and_hydration_does_not_read_archive(env, monkeypatch):
    store, client, _ = env
    archive(store, "ancient", facts="OLD_PRIVATE_ARCHIVE_CANARY")
    for i in range(12):
        archive(store, f"recent-{i}", at=100+i, facts="later session")
    # Existing search lazily initializes thread_continuity via dyn.list.
    # Warm that pre-existing catalog behavior before checking archive reads.
    client.app.state.dynstores.list("tara")
    before = list(store.db.iterdump())
    client.get("/profiles/tara/context/search", params={"q": "OLD_PRIVATE_ARCHIVE_CANARY"})
    assert before == list(store.db.iterdump())
    def forbidden(*args, **kwargs):
        raise AssertionError("hydration must not call archive search")
    monkeypatch.setattr(store, "search_closeouts", forbidden)
    for options in [None, {"mode": "forum"}, {"mode": "editor"},
                    {"mode": "actor"}, {"mode": "workbench"},
                    {"mode": "secondary", "session": {"available_sections": ["identity"]}}]:
        packet = client.post("/profiles/tara/session", json=options)
        assert packet.status_code == 200, packet.text
        assert "OLD_PRIVATE_ARCHIVE_CANARY" not in packet.text


def test_bridge_and_mcp_forward_closeout_source(env):
    store, client, path = env
    archive(store, "first", facts="ancestral marker")
    bridge = ToolBridge(client=client)
    runner = MCPToolRunner(bridge, closeout_signing_key="test",
                          closeout_code_state=CloseoutCodeState(str(path / "codes.db")))
    hits = runner.call("search_context", {"profile_id": "tara", "query": "ancestral marker"})
    assert hits[0]["source_type"] == "closeout"
    Draft202012Validator(CONTEXT_RESULT).validate(hits[0])


def test_scoped_credentials_and_retired_profile_route(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), auth_enabled=True)
    store = app.state.store
    archive(store, "ours", facts="same historical phrase")
    archive(store, "theirs", profile="sidra", facts="same historical phrase")
    with store.db:
        store.db.execute("INSERT INTO profile_id_redirects VALUES (?,?)", ("old_tara", "tara"))
    access = app.state.access
    principal = access.create_principal("app", "tara search only")
    access.create_credential(principal["id"], "test", "test-scoped-secret")
    access.grant(principal["id"], "search", profile_id="tara")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-scoped-secret"}
        assert client.get("/profiles/tara/context/search", params={"q": "phrase"}, headers=headers).status_code == 403
        access.grant(principal["id"], "records:read", profile_id="tara")
        for profile in ["tara", "old_tara"]:
            response = client.get(f"/profiles/{profile}/context/search", params={"q": "phrase"}, headers=headers)
            assert response.status_code == 200, response.text
            assert [h["item"]["id"] for h in response.json()] == ["ours"]
        assert client.get("/profiles/sidra/context/search", params={"q": "phrase"}, headers=headers).status_code == 403
        assert client.get("/profiles/tara/context/search", params={"q": "phrase"}).status_code == 401
