import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from profile_os.api import create_app
from profile_os.bridge import ToolBridge
from profile_os.shared_files import SharedFiles, SharedFileExists
from profile_os.mcp_server import MCP_TOOLS, MCPToolRunner, CloseoutCodeState
from profile_os.tool_schemas import SHARED_FILE_META, SHARED_FILE_CONTENT, SHARED_FILE_LIST


@pytest.fixture
def app(tmp_path):
    return create_app(data_dir=str(tmp_path/'data'))


def test_cross_companion_visibility_and_immutable_even_for_author(app):
    with TestClient(app) as c:
        b=ToolBridge(client=c)
        meta=b.create_shared_file('tara','tara-note-v1.md','hello 🦊')
        Draft202012Validator(SHARED_FILE_META).validate(meta)
        assert meta['created_by']=='tara'
        assert meta['sha256']==hashlib.sha256('hello 🦊'.encode()).hexdigest()
        assert meta['size']==len('hello 🦊'.encode())
        read=b.read_shared_file('sidra','tara-note-v1.md')
        Draft202012Validator(SHARED_FILE_CONTENT).validate(read)
        assert read['content']=='hello 🦊'
        listing=b.list_shared_files('sidra')
        Draft202012Validator(SHARED_FILE_LIST).validate(listing)
        assert listing['items']==[meta]
        for profile in ['tara','sidra']:
            assert c.post(f'/profiles/{profile}/shared-files/tara-note-v1.md',json={'content':'overwrite'}).status_code==409
            for method in ['put','patch','delete']:
                assert getattr(c,method)(f'/profiles/{profile}/shared-files/tara-note-v1.md').status_code==405
        # Existing private file endpoints cannot touch shared artifacts.
        b.write_file('tara','tara-note-v1.md','private')
        b.delete_file('tara','tara-note-v1.md')
        assert b.read_shared_file('sidra','tara-note-v1.md')==read
        assert b.list_files('tara')==[]
        assert 'shared shelf' in b.start_session('tara')['system_contracts']['companion']


def test_concurrent_publication_has_one_winner_and_no_partial_reads(app):
    shelf=SharedFiles(app.state.store)
    def create(i):
        try:
            return shelf.create('tara','race.md',str(i)*20000)
        except SharedFileExists:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(create,range(8)))
    assert sum(r is not None for r in results)==1
    read=shelf.read('sidra','race.md')
    assert read['sha256']==hashlib.sha256(read['content'].encode()).hexdigest()
    assert len(read['content'])==20000
    assert not list(shelf.directory.glob('.pending-*'))


def test_pagination_and_validation(app):
    with TestClient(app) as c:
        b=ToolBridge(client=c)
        for name in ['a.md','b.md','c.md','.pending-note.md']:
            b.create_shared_file('tara',name,'')
        page=b.list_shared_files('sidra',limit=2)
        assert [x['filename'] for x in page['items']]==['.pending-note.md','a.md']
        assert page['next_after']=='a.md'
        page=b.list_shared_files('sidra',limit=2,after=page['next_after'])
        assert [x['filename'] for x in page['items']]==['b.md','c.md'] and page['next_after'] is None
        assert c.get('/profiles/sidra/shared-files/missing.md').status_code==404
        for name in ['bad..md','a%5Cb','bad%0A']:
            assert c.post('/profiles/tara/shared-files/'+name,json={'content':'x'}).status_code==422
        assert c.post('/profiles/tara/shared-files/huge.md',json={'content':'é'*131073}).status_code==422
        assert c.get('/profiles/tara/shared-files',params={'limit':201}).status_code==422
        assert c.get('/profiles/tara/shared-files',params={'after':'../'}).status_code==422
        assert c.get('/profiles/absent/shared-files').status_code==404


def test_readonly_and_permissions(tmp_path):
    app=create_app(data_dir=str(tmp_path/'data'),auth_enabled=True)
    access=app.state.access
    p=access.create_principal('bridge','reader')
    access.create_credential(p['id'],'k','reader-secret')
    access.grant(p['id'],'search',profile_id='sidra')
    shelf=SharedFiles(app.state.store)
    shelf.create('tara','public.md','public')
    with TestClient(app) as c:
        headers={'Authorization':'Bearer reader-secret'}
        assert c.get('/profiles/sidra/shared-files/public.md',headers=headers).status_code==200
        assert c.get('/profiles/tara/shared-files',headers=headers).status_code==403
        assert c.post('/profiles/sidra/shared-files/new.md',headers=headers,json={'content':'x'}).status_code==403
        assert c.get('/profiles/sidra/shared-files').status_code==401
    readonly=create_app(data_dir=str(tmp_path/'data'),read_only=True)
    with TestClient(readonly) as c:
        assert c.get('/profiles/sidra/shared-files/public.md').status_code==200
        assert c.post('/profiles/sidra/shared-files/new.md',json={'content':'x'}).status_code==503


def test_mcp_catalog_and_dispatch(app):
    with TestClient(app) as c:
        runner=MCPToolRunner(ToolBridge(client=c), closeout_signing_key="test",
            closeout_code_state=CloseoutCodeState(str(app.state.store.data_dir/"codes.db")))
        # Runner's public tool dispatcher is exercised below.
        tools={t['name']:t for t in MCP_TOOLS}
        assert not tools['create_shared_file']['annotations']['destructiveHint']
        assert not tools['create_shared_file']['annotations']['readOnlyHint']
        assert tools['read_shared_file']['annotations']['readOnlyHint']
        assert tools['list_shared_files']['annotations']['readOnlyHint']
        assert 'delete_shared_file' not in tools and 'update_shared_file' not in tools

        created=runner.call('create_shared_file', {'profile_id':'tara','filename':'mcp.md','content':'hello'})
        assert created['created_by']=='tara'
        assert runner.call('read_shared_file',{'profile_id':'sidra','filename':'mcp.md'})['content']=='hello'
        assert runner.call('list_shared_files',{'profile_id':'sidra'})['items']==[created]
        for name,result in [('create_shared_file',created),('read_shared_file',runner.call('read_shared_file',{'profile_id':'sidra','filename':'mcp.md'}))]:
            Draft202012Validator(tools[name]['outputSchema']).validate(result)


def test_failed_publication_leaves_no_entry_and_restart_preserves_content(app,monkeypatch):
    from profile_os import shared_files
    shelf=SharedFiles(app.state.store)
    original=shared_files.os.link
    def fail(*args,**kwargs):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(shared_files.os,'link',fail)
    with pytest.raises(OSError,match='simulated disk failure'):
        shelf.create('tara','failure.md','not published')
    assert shelf.list('sidra')['items']==[]
    assert list(shelf.directory.iterdir())==[]
    monkeypatch.setattr(shared_files.os,'link',original)
    shelf.create('tara','saved.md','persisted')
    restarted=create_app(data_dir=str(app.state.store.data_dir))
    assert SharedFiles(restarted.state.store).read('sidra','saved.md')['content']=='persisted'


def test_shared_read_does_not_follow_symlink_or_publish_over_one(app,tmp_path):
    shelf=SharedFiles(app.state.store)
    private=tmp_path/'private.json'
    private.write_text('{"content":"secret"}')
    (shelf.directory/'secret.md.json').symlink_to(private)
    with pytest.raises(OSError):
        shelf.read('sidra','secret.md')
    with pytest.raises(SharedFileExists):
        shelf.create('tara','secret.md','overwrite')
    assert 'secret' in private.read_text()
    assert shelf.list('sidra')['items']==[]
