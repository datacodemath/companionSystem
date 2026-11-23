import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from profile_os.hydration import model_facing_hydration
from profile_os.tool_schemas import MODEL_HYDRATION_SESSION, START_SESSION


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "hydration_packets.json").read_text()
)


@pytest.mark.parametrize("name", ["conversation", "workbench"])
def test_representative_packet_matches_reproducible_after_fixture(name):
    pair = FIXTURES[name]
    packet = model_facing_hydration(pair["before"])
    assert packet == pair["after"]
    Draft202012Validator(MODEL_HYDRATION_SESSION).validate(packet)
    Draft202012Validator(START_SESSION).validate(packet)


def test_conversation_keeps_canonical_identity_permissions_state_and_memories():
    packet = model_facing_hydration(FIXTURES["conversation"]["before"])
    assert packet["who_you_are"] == "Limo, a practical thunder gecko coding companion."
    assert packet["signature"] == "🦎🔧⚡"
    assert packet["profile"]["allowed_tools"] == ["search_context", "search_memories"]
    assert packet["profile"]["memory_policy"] == {"max_recent": 10}
    assert packet["system_contracts"]["companion"]
    assert packet["identity"]
    assert packet["compact_state"].startswith("Current project:")
    assert len(packet["memories"]) == 2
    assert packet["recent_work"] == "I have been working on slim hydration packets."
    assert packet["recent_session_log"]
    assert [p["id"] for p in packet["companion_directory"]] == ["tara"]


def test_workbench_keeps_task_permissions_safety_and_actionable_warning():
    packet = model_facing_hydration(FIXTURES["workbench"]["before"])
    invocation = packet["invocation"]
    assert invocation["mode"] == "workbench"
    assert invocation["task"]["purpose"] == "Audit hydration"
    assert "Respect existing permissions" in invocation["instructions"]
    assert "not grants" in invocation["permissions"]
    assert "no automatic memory" in invocation["persistence"]
    assert packet["warnings"] == [
        "Resource/repo references are host-supplied labels, not loaded content."
    ]


def test_projection_preserves_existing_warnings_and_appends_hydration_warnings():
    source = deepcopy(FIXTURES["workbench"]["before"])
    source["warnings"] = ["Host warning already present."]

    packet = model_facing_hydration(source)

    assert packet["warnings"] == [
        "Host warning already present.",
        "Resource/repo references are host-supplied labels, not loaded content.",
    ]
    assert source["warnings"] == ["Host warning already present."]


@pytest.mark.parametrize("name", ["conversation", "workbench"])
def test_model_packet_has_no_delivery_bookkeeping_or_duplicate_registry_identity(name):
    packet = model_facing_hydration(FIXTURES[name]["before"])
    assert "hydration" not in packet
    assert "selection" not in packet
    assert "signature" not in packet["profile"]
    assert "aliases" not in packet["profile"]
    if "invocation" in packet:
        assert "policy_version" not in packet["invocation"]
        assert "requested_mode" not in packet["invocation"]
        assert "isolation" not in packet["invocation"]
