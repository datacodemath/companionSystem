import pytest
from profile_os.storage import validate_signature, Store
from profile_os.errors import MalformedRecord
from profile_os.api import PromptEditProposeIn

@pytest.mark.parametrize('value',['♔♕♖♗♘','♙','♚♛♜♝♞','♟','♟️','♔🦊','∑∞→⚙','✦☉☽','👨‍👩‍👧‍👦','🇨🇴','👍🏽','🧵🦊',''])
def test_accepts_symbols_and_existing_emoji(value):
    assert validate_signature(value)==value
    assert PromptEditProposeIn(signature=value).signature==value

@pytest.mark.parametrize('value',['♔♕♖♗♘♙','hello','a','123',' ','♔ ♕','♔\n','\u200d','\u200b','\u202e','\ufe0f','\u0301','<script>'])
def test_rejects_text_controls_and_too_many_clusters(value):
    with pytest.raises(MalformedRecord): validate_signature(value)

def test_persisted_signature_and_prompt_roundtrip(tmp_path):
    store=Store(tmp_path/'data')
    store.create_profile('chess','Chess','Chess companion','Play',signature='♖🦊')
    assert store.get_profile('chess')['signature']=='♖🦊'
    store.update_prompts('chess',signature='♜♞')
    assert store.get_profile('chess')['signature']=='♜♞'


def test_output_schema_counts_no_codepoints():
    from profile_os.tool_schemas import PROFILE
    from jsonschema import Draft202012Validator
    Draft202012Validator(PROFILE['properties']['signature']).validate('👨‍👩‍👧‍👦♖')


def test_prompt_proposal_accepts_chess_but_keeps_approval_gate(tmp_path):
    from fastapi.testclient import TestClient
    from profile_os.api import create_app
    app=create_app(data_dir=str(tmp_path/'data'))
    with TestClient(app) as client:
        before=app.state.store.get_profile('tara')['signature']
        r=client.post('/profiles/tara/prompt',json={'signature':'♜🦊'})
        assert r.status_code==201,r.text
        assert r.json()['status']=='pending'
        assert app.state.store.get_profile('tara')['signature']==before
        assert client.post('/profiles/tara/prompt',json={'signature':'letters'}).status_code==422
