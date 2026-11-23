"""Sleep cursors advance only after success and safely tolerate retries."""

from fastapi.testclient import TestClient
import pytest

from profile_os.api import create_app
from profile_os.bridge import ToolBridge
from profile_os.errors import MalformedRecord
from profile_os.storage import Store


def test_store_sleep_checkpoint_is_monotonic_and_idempotent(tmp_path):
    store = Store(tmp_path / "data")
    store.create_profile("limo", "Limo", "Gecko.", "Build software.")

    assert store.get_sleep_checkpoint("limo")["closeout_cursor"] == 0
    first = store.complete_sleep("limo", "dream:limo:10", 41, completed_at=1_800_000_000)
    assert first == {
        "profile_id": "limo",
        "closeout_cursor": 41,
        "last_request_id": "dream:limo:10",
        "last_successful_at": 1_800_000_000,
        "duplicate": False,
    }

    retry = store.complete_sleep("limo", "dream:limo:10", 41, completed_at=1_800_000_100)
    assert retry["duplicate"] is True
    assert retry["last_successful_at"] == 1_800_000_000

    with pytest.raises(MalformedRecord, match="different cursor"):
        store.complete_sleep("limo", "dream:limo:10", 42, completed_at=1_800_000_200)
    with pytest.raises(MalformedRecord, match="move backwards"):
        store.complete_sleep("limo", "dream:limo:9", 40, completed_at=1_800_000_300)

    next_run = store.complete_sleep("limo", "dream:limo:11", 41, completed_at=1_800_000_400)
    assert next_run["closeout_cursor"] == 41
    assert next_run["last_request_id"] == "dream:limo:11"


def test_sleep_checkpoint_api_uses_remember_grant_and_bridge_round_trips(tmp_path):
    app = create_app(data_dir=str(tmp_path / "profile_data"), auth_enabled=True, do_seed=False)
    app.state.store.create_profile("limo", "Limo", "Gecko.", "Build software.")
    principal = app.state.access.create_principal("bridge", "Sleep worker")
    app.state.access.create_credential(principal["id"], "test", "sleep-worker-token")
    app.state.access.grant(principal["id"], "remember", profile_id="limo")

    with TestClient(app) as client:
        assert client.get("/profiles/limo/sleep-checkpoint").status_code == 401
        bridge = ToolBridge(bearer="sleep-worker-token", client=client)
        initial = bridge.get_sleep_checkpoint("limo")
        assert initial["closeout_cursor"] == 0
        saved = bridge.complete_sleep("limo", "dream:limo:1", 7, completed_at=1_800_000_000)
        assert saved["duplicate"] is False
        replay = bridge.complete_sleep("limo", "dream:limo:1", 7, completed_at=1_800_000_100)
        assert replay["duplicate"] is True
        assert bridge.get_sleep_checkpoint("limo")["closeout_cursor"] == 7
