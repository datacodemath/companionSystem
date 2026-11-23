import json
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from profile_os.api import create_app
from profile_os.bridge import ToolBridge, ToolBridgeError
from profile_os.mcp_server import MCPToolRunner, CloseoutCodeState, MCP_TOOLS
from profile_os.tool_schemas import MODEL_HYDRATION_SESSION


@pytest.fixture
def env(tmp_path):
    identity=tmp_path/'identity.md'
    identity.write_text('OWNER_PRIVATE_CANARY')
    app=create_app(data_dir=str(tmp_path/'data'),identity_file=str(identity))
    app.state.store.remember('tara', {'kind':'note','content':'MEMORY_PRIVATE_CANARY','tags':[]})
    with TestClient(app) as client:
        bridge=ToolBridge(client=client)
        runner=MCPToolRunner(bridge,closeout_signing_key='test',
            closeout_code_state=CloseoutCodeState(str(tmp_path/'closeout.db')))
        yield app,client,bridge,runner


def check(packet):
    Draft202012Validator(MODEL_HYDRATION_SESSION).validate(packet)


def test_legacy_packets_unchanged(env):
    app,c,b,r=env
    for args in [{'profile_id':'tara'},{'profile_id':'tara','mode':'conversation'}]:
        p=r.call('summon_companion',args)
        check(p)
        assert 'hydration' not in p and 'invocation' not in p
        assert 'selection' not in p
        assert p['profile']['id']=='tara'
        assert p['identity']=='OWNER_PRIVATE_CANARY'
        assert p['memories'][0]['content']=='MEMORY_PRIVATE_CANARY'
    forum=r.call('summon_companion',{'profile_id':'tara','mode':'forum'})
    check(forum)
    assert forum['identity'] is None
    assert 'recent_session_log' not in forum
    assert 'thread_continuity' in forum


@pytest.mark.parametrize('mode',['editor','actor','workbench'])
def test_modes_keep_voice_avoid_private_autobiography_and_no_side_effects(env,mode,monkeypatch):
    app,c,b,r=env
    # Focused invocations must not even load private episodic context.
    def forbidden(*a,**k): raise AssertionError('private context loaded')
    monkeypatch.setattr(app.state.store,'boot',forbidden)
    monkeypatch.setattr(app.state.store,'recent_exchanges',forbidden)
    monkeypatch.setattr(app.state.store,'recent_session_log',forbidden)
    p=r.call('summon_companion',{'profile_id':'tara','mode':mode,'task':{'brief':'task only'}})
    check(p)
    assert p['who_you_are']==app.state.store._prompt_sections('tara')['who_you_are']
    assert p['voice']==app.state.store._prompt_sections('tara')['voice']
    assert p['what_you_do']
    assert 'OWNER_PRIVATE_CANARY' not in json.dumps(p)
    assert 'MEMORY_PRIVATE_CANARY' not in json.dumps(p)
    assert not {'memories','compact_state','recent_exchanges','recent_session_log','companion_directory','data_sources'} & p.keys()
    assert p['invocation']['mode']==mode
    assert p['invocation']['task']['brief']=='task only'
    assert 'hydration' not in p and 'selection' not in p
    assert 'signature' not in p['profile'] and 'aliases' not in p['profile']
    assert p['profile']['allowed_tools'] is not None
    assert app.state.store.search('tara','task only')==[]


def test_delta_uses_declaration_without_model_facing_receipt_or_section_inventory(env):
    app,c,b,r=env
    p=r.call('summon_companion',{'profile_id':'tara','mode':'secondary',
        'session':{'id':'host','available_sections':['identity','system_contracts','companion_directory']}})
    check(p)
    assert p['invocation']['mode']=='conversation'
    assert 'hydration' not in p and 'selection' not in p
    assert not {'identity','system_contracts','companion_directory'} & p.keys()
    assert p['who_you_are'] and p['what_you_do']
    assert p['voice']==app.state.store._prompt_sections('tara')['voice']
    assert p['warnings']
    assert 'retention is not verified' in p['warnings'][0]
    full=r.call('summon_companion',{'profile_id':'tara','mode':'conversation','hydration':'full'})
    check(full)
    assert full['identity']=='OWNER_PRIVATE_CANARY'
    assert 'system_contracts' in full
    for args in [{'profile_id':'tara','hydration':'delta'},
                 {'profile_id':'tara','hydration':'delta','session':{'context_reset':True,'available_sections':['identity']}}]:
        p=r.call('summon_companion',args)
        assert 'hydration' not in p
        assert p['identity']=='OWNER_PRIVATE_CANARY'


def test_editor_actor_workbench_and_unresolved_references(env):
    app,c,b,r=env
    editor=r.call('summon_companion',{'profile_id':'tara','mode':'editor'})
    assert 'not votes' in editor['invocation']['instructions']
    actor=r.call('summon_companion',{'profile_id':'tara','mode':'actor',
        'task':{'character':{'id':'Mara','knowledge_scope':'only the village','motivation':'find shelter','scene':'rain'}}})
    assert 'isolation' not in actor['invocation']
    assert actor['invocation']['task']['character']['id']=='Mara'
    assert 'director' in actor['invocation']['instructions']
    work=r.call('summon_companion',{'profile_id':'tara','mode':'workbench','task':{
        'resource_refs':['https://127.0.0.1/no-fetch'],
        'environment':{'kind':'codex','repo_ref':'/private/no-read','diagnostics':'test failed',
                       'commands':['touch /tmp/must-not-run'],'authorization_scope':'deploy'}}})
    assert work['invocation']['task']['environment']['diagnostics']=='test failed'
    assert 'not a grant' in work['invocation']['instructions']
    assert any('not loaded' in x for x in work['warnings'])
    assert 'unchanged' in work['invocation']['permissions']


@pytest.mark.parametrize('options',[
    {'mode':'invalid'}, {'mode':'secondary','hydration':'full'},
    {'hydration':'invalid'}, {'mode':'editor','task':{'character':{'id':'x'}}},
    {'mode':'actor','task':{'environment':{'kind':'codex'}}},
    {'session':{'available_sections':['voice']}}, {'task':{'brief':'x'*8193}},
    {'task':{'resource_refs':['ref']*21}}, {'unexpected':True},
])
def test_invalid_options_rejected(env,options):
    app,c,b,r=env
    assert c.post('/profiles/tara/session',json=options).status_code==422
    with pytest.raises(ToolBridgeError) as e:
        r.call('summon_companion',{'profile_id':'tara',**options})
    assert e.value.status_code==422


def test_new_modes_require_existing_permissions(tmp_path):
    app=create_app(data_dir=str(tmp_path/'data'),auth_enabled=True)
    a=app.state.access
    p=a.create_principal('bridge','limited')
    a.create_credential(p['id'],'k','limited-secret')
    a.grant(p['id'],'boot',profile_id='tara')
    with TestClient(app) as c:
        h={'Authorization':'Bearer limited-secret'}
        assert c.post('/profiles/tara/session',json={'mode':'editor'}).status_code==401
        assert c.post('/profiles/sidra/session',json={'mode':'editor'},headers=h).status_code==403
        r=c.post('/profiles/tara/session',json={'mode':'workbench','task':{'environment':{'authorization_scope':'edit'}}},headers=h)
        assert r.status_code==200
        assert c.put('/profiles/tara/files/blocked.md',headers=h,json={'content':'no'}).status_code==403
        assert c.post('/profiles/tara/session',json={'hydration':'delta','session':{'available_sections':['system_contracts']}},headers=h).status_code==200


def test_catalog_and_five_distinct_companions(env):
    app,c,b,r=env
    tool=next(t for t in MCP_TOOLS if t['name']=='summon_companion')
    assert {'editor','actor','workbench','secondary'} <= set(tool['inputSchema']['properties']['mode']['enum'])
    names=['tara','sidra','hilo','aster','limo']
    # Minimal seed may omit named live profiles, so create peers for this fixture.
    existing={p['id'] for p in app.state.store.list_profiles()}
    for name in names:
        if name not in existing:
            app.state.store.create_profile(name,name,f'I am {name}',f'Work as {name}')
            app.state.store.update_prompts(name,voice=f'{name} voice')
    packets=[r.call('summon_companion',{'profile_id':name,'mode':'editor','hydration':'delta',
            'session':{'available_sections':['system_contracts']}}) for name in names]
    for p in packets: check(p)
    assert len({p['profile']['id'] for p in packets})==5
    assert len({p['who_you_are'] for p in packets})==5
    assert sum(len(json.dumps(p)) for p in packets) < sum(len(json.dumps(b.start_session(name))) for name in names)
