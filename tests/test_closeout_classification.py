import sqlite3

from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.errors import MalformedRecord
from profile_os.storage import Store


def _profile(store, profile_id):
    store.create_profile(profile_id, profile_id.title(), "A companion.", "Build and think.")


def test_workbench_closeout_is_archived_without_entering_conversation_state(tmp_path):
    store = Store(tmp_path / "data")
    _profile(store, "limo")
    conversation = store.closeout("limo", "chat facts", "warm texture", "User: hello")
    state_before = store.boot("limo")["compact_state"]
    log_before = store.recent_session_log("limo")
    exchanges_before = store.recent_exchanges("limo")

    workbench = store.closeout(
        "limo", "task facts", "task texture", "Agent: completed", closeout_type="workbench")

    assert conversation["closeout_type"] == "conversation"
    assert workbench["closeout_type"] == "workbench"
    assert store.boot("limo")["compact_state"] == state_before
    assert store.recent_session_log("limo") == log_before
    assert store.recent_exchanges("limo") == exchanges_before
    assert [row["id"] for row in store.recent_closeouts("limo")] == [conversation["id"]]
    assert [row["id"] for row in store.search_closeouts("limo", "task facts")] == []
    assert [row["id"] for row in store.list_workbench_closeouts("limo")["items"]] == [workbench["id"]]
    assert len((store.profiles_dir / "limo" / "closeouts.jsonl").read_text().splitlines()) == 1

    try:
        store.closeout("limo", "x", "y", "z", closeout_type="forum")
    except MalformedRecord as error:
        assert "closeout_type" in str(error)
    else:
        raise AssertionError("invalid closeout type was accepted")


def test_legacy_closeouts_default_to_conversation_on_additive_migration(tmp_path):
    data_dir = tmp_path / "data"
    store = Store(data_dir)
    _profile(store, "limo")
    old = store.closeout("limo", "legacy facts", "legacy texture", "User: preserved")
    with store.db:
        store.db.execute("DROP INDEX idx_closeouts_profile_type_page")
        store.db.execute("ALTER TABLE closeouts RENAME TO closeouts_new")
        store.db.execute(
            "CREATE TABLE closeouts (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, "
            "notes TEXT NOT NULL, facts TEXT NOT NULL DEFAULT '', texture TEXT NOT NULL DEFAULT '', "
            "exchange TEXT NOT NULL DEFAULT '', new_state TEXT NOT NULL, created_at REAL NOT NULL)")
        store.db.execute(
            "INSERT INTO closeouts "
            "(id, profile_id, notes, facts, texture, exchange, new_state, created_at) "
            "SELECT id, profile_id, notes, facts, texture, exchange, new_state, created_at "
            "FROM closeouts_new")
        store.db.execute("DROP TABLE closeouts_new")
    store.db.close()

    migrated = Store(data_dir)
    row = migrated.db.execute("SELECT closeout_type FROM closeouts WHERE id=?", (old["id"],)).fetchone()
    assert row["closeout_type"] == "conversation"
    assert migrated.recent_session_log("limo")[0]["summary"].startswith("## Facts")
    assert migrated.list_workbench_closeouts("limo")["items"] == []


def test_workbench_closeout_cursor_and_time_window_pagination(tmp_path):
    store = Store(tmp_path / "data")
    _profile(store, "limo")
    _profile(store, "tara")
    ids = []
    for index, at in enumerate((1.0, 2.0, 2.0, 3.0), start=1):
        row = store.closeout(
            "limo", f"task {index}", "texture", "Agent: done", closeout_type="workbench")
        ids.append(row["id"])
        with store.db:
            store.db.execute("UPDATE closeouts SET created_at=? WHERE id=?", (at, row["id"]))
    conversation = store.closeout("limo", "dialog only", "texture", "User: hi")
    with store.db:
        store.db.execute("UPDATE closeouts SET created_at=2.5 WHERE id=?", (conversation["id"],))
    other = store.closeout("tara", "other profile", "texture", "Agent: done", closeout_type="workbench")
    with store.db:
        store.db.execute("UPDATE closeouts SET created_at=2.25 WHERE id=?", (other["id"],))

    first = store.list_workbench_closeouts("limo", since=2.0, until=4.0, limit=1)
    second = store.list_workbench_closeouts(
        "limo", since=2.0, until=4.0, cursor=first["next_cursor"], limit=1)
    third = store.list_workbench_closeouts(
        "limo", since=2.0, until=4.0, cursor=second["next_cursor"], limit=1)
    assert len(first["items"]) == len(second["items"]) == len(third["items"]) == 1
    page_ids = [first["items"][0]["id"], second["items"][0]["id"], third["items"][0]["id"]]
    assert page_ids == sorted(ids[1:], key=lambda value: (2.0 if value in ids[1:3] else 3.0, value), reverse=True)
    assert len(set(page_ids)) == 3
    assert third["next_cursor"] is None
    assert all(item["closeout_type"] == "workbench" for page in (first, second, third) for item in page["items"])
    assert [item["id"] for item in store.list_workbench_closeouts(
        "limo", since=3.0, until=4.0)["items"]] == [ids[3]]
    assert [item["id"] for item in store.list_workbench_closeouts(
        "limo", since=2.0, until=3.0)["items"]] == sorted(ids[1:3], reverse=True)

    try:
        store.list_workbench_closeouts("limo", cursor="not-a-cursor")
    except MalformedRecord as error:
        assert "cursor" in str(error)
    else:
        raise AssertionError("malformed cursor was accepted")


def test_workbench_closeout_http_authorization_and_pagination(tmp_path):
    app = create_app(data_dir=str(tmp_path / "api"), auth_enabled=True, do_seed=False)
    store = app.state.store
    _profile(store, "limo")
    _profile(store, "tara")
    for index in range(2):
        store.closeout("limo", f"task {index}", "texture", "Agent: done", closeout_type="workbench")
    principal = app.state.access.create_principal("bridge", "Closeout reader")
    token = "closeout-reader-test-token"
    app.state.access.create_credential(principal["id"], "test", token)
    app.state.access.grant(principal["id"], "search", profile_id="limo")
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        path = "/profiles/limo/closeouts/workbench"
        assert client.get(path).status_code == 401
        assert client.get("/profiles/tara/closeouts/workbench", headers=headers).status_code == 403
        first = client.get(path, headers=headers, params={"limit": 1})
        assert first.status_code == 200
        first_page = first.json()
        assert len(first_page["items"]) == 1
        second = client.get(path, headers=headers, params={"limit": 1, "cursor": first_page["next_cursor"]})
        assert second.status_code == 200
        second_page = second.json()
        assert len(second_page["items"]) == 1
        assert second_page["items"][0]["id"] != first_page["items"][0]["id"]
        assert second_page["next_cursor"] is None


def test_closeout_http_mode_defaults_and_workbench_does_not_change_state(tmp_path):
    app = create_app(data_dir=str(tmp_path / "api"), auth_enabled=True, do_seed=False)
    store = app.state.store
    _profile(store, "limo")
    principal = app.state.access.create_principal("bridge", "Closeout writer")
    token = "closeout-writer-test-token"
    app.state.access.create_credential(principal["id"], "test", token)
    app.state.access.grant(principal["id"], "closeout", profile_id="limo")
    headers = {"Authorization": f"Bearer {token}"}
    body = {"facts": "ordinary", "texture": "clear", "exchange": "User: hi"}

    with TestClient(app) as client:
        ordinary = client.post("/profiles/limo/closeout", headers=headers, json=body)
        assert ordinary.status_code == 201
        assert ordinary.json()["closeout_type"] == "conversation"
        state_before = store.boot("limo")["compact_state"]
        workbench = client.post(
            "/profiles/limo/closeout", headers=headers,
            json={**body, "facts": "agentic handoff", "closeout_type": "workbench"},
        )
        assert workbench.status_code == 201
        assert workbench.json()["closeout_type"] == "workbench"
        assert store.boot("limo")["compact_state"] == state_before

