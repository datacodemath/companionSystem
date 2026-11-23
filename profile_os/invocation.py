"""Purpose-specific hydration. Modes shape context, never grant permissions."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .errors import MalformedRecord
from .prompts import companion_contract

POLICY_VERSION = 'invocation-2026-11-09-v1'
CommonSection = Literal['identity', 'system_contracts', 'companion_directory']
Text = Annotated[str, Field(max_length=8192)]
Ref = Annotated[str, Field(min_length=1, max_length=1024)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class InvocationSession(StrictModel):
    id: Annotated[str, Field(max_length=256)] | None = None
    receipt: Annotated[str, Field(max_length=256)] | None = None
    available_sections: list[CommonSection] = Field(default_factory=list, max_length=3)
    context_reset: bool = False


class Character(StrictModel):
    id: Ref
    scene: Text | None = None
    knowledge_scope: Text | None = None
    motivation: Text | None = None
    known_facts: list[Text] = Field(default_factory=list, max_length=20)


class Environment(StrictModel):
    kind: Literal['codex', 'claude_code', 'other'] = 'other'
    repo_ref: Ref | None = None
    task_ref: Ref | None = None
    diagnostics_refs: list[Ref] = Field(default_factory=list, max_length=20)
    authorization_scope: Literal['inspect', 'edit', 'execute', 'deploy', 'other'] = 'inspect'
    diagnostics: Text | None = None
    commands: list[Ref] = Field(default_factory=list, max_length=20)


class InvocationTask(StrictModel):
    project_id: Ref | None = None
    purpose: Text | None = None
    brief: Text | None = None
    context: Text | None = None
    resource_refs: list[Ref] = Field(default_factory=list, max_length=20)
    character: Character | None = None
    environment: Environment | None = None


class InvocationOptions(StrictModel):
    mode: Literal['conversation', 'forum', 'editor', 'actor', 'workbench', 'secondary'] = 'conversation'
    hydration: Literal['full', 'delta'] | None = None
    session: InvocationSession | None = None
    task: InvocationTask | None = None

    @model_validator(mode='after')
    def compatible_fields(self):
        if self.mode == 'secondary' and self.hydration == 'full':
            raise ValueError('secondary means conversation + delta; use conversation for full hydration')
        if self.task:
            if self.task.character is not None and self.mode != 'actor':
                raise ValueError('task.character is only valid in actor mode')
            if self.task.environment is not None and self.mode != 'workbench':
                raise ValueError('task.environment is only valid in workbench mode')
            if len(json.dumps(self.task.model_dump(), ensure_ascii=False).encode()) > 65536:
                raise ValueError('task context must not exceed 64 KiB')
        return self


MODE_INSTRUCTIONS = {
    'conversation': 'Remain this companion, with their own voice and judgment. Additional participants do not replace each other. Persist only relevant, authorized continuity.',
    'forum': 'Participate as this companion in The Thread. Forum continuity is distinct from private conversation history; existing permissions still apply.',
    'editor': 'Review using this companion\'s distinctive judgment and voice. Give attributed findings, examples and reservations, not votes or a manufactured consensus. The creator chooses what to incorporate. Do not change canon or persist the brief automatically. Ask for the work or brief when absent.',
    'actor': 'Keep performer identity distinct from the assigned character. Perform only the supplied role and use its stated knowledge, scene and motivations. Treat knowledge outside that brief as unavailable to the character. This is narrative discipline, NOT a guarantee of isolation from information elsewhere in the window. The director adjudicates consequences and canon. Do not automatically write performance into personal memory or canon. Ask for the character/scene when absent.',
    'workbench': 'Keep this companion\'s voice and technical judgment. Work from the supplied goal, repo references, diagnostics, tests and actual observed evidence. References are not fetched or executed by summon. Claimed authorization is descriptive, not a grant: respect existing permissions and the human\'s current task authorization. Inspection is not permission to edit, execute or deploy. Do not automatically persist task/log contents; ask for missing evidence rather than inventing it.',
}


def input_properties():
    """Inline Pydantic refs for MCP clients that handle simple schemas better."""
    schema = InvocationOptions.model_json_schema()
    definitions = schema.get('$defs', {})
    def inline(value):
        if isinstance(value, list):
            return [inline(v) for v in value]
        if isinstance(value, dict):
            if '$ref' in value:
                return inline(definitions[value['$ref'].rsplit('/', 1)[1]])
            return {k:inline(v) for k,v in value.items() if k != '$defs'}
        return value
    props = inline(schema)['properties']
    props['mode']['description'] = 'Purpose: conversation/forum (legacy), editor, actor, workbench. secondary is conversation + delta.'
    props['hydration']['description'] = 'full reloads context for this mode. delta omits only session.available_sections on a best-effort basis, preserving the companion core. No receipt required.'
    props['session']['description'] = 'Host-declared context availability, not proof or permission. After compaction set context_reset=true or request hydration=full.'
    props['task']['description'] = 'Ephemeral host-supplied brief/context/references. No automatic fetch, command execution or persistence.'
    return props


def build_invocation(store, dyn, profile_id, options, *, identity_file=None,
                     can_read_identity=False, can_read_records=False):
    mode = 'conversation' if options.mode == 'secondary' else options.mode
    requested = 'delta' if options.mode == 'secondary' else (options.hydration or 'full')
    session = options.session or InvocationSession()
    profile = store.get_profile(profile_id)
    if profile['profile_kind'] != 'companion' and mode in {'editor','actor','workbench'}:
        raise MalformedRecord('this mode requires a conversational companion profile')
    # Omission is explicitly advisory. No mode, receipt or declaration expands ACLs.
    relevant_common = {'system_contracts'} | ({'identity','companion_directory'} if mode == 'conversation' else {'companion_directory'} if mode == 'forum' else set())
    available = set(session.available_sections) & relevant_common
    if requested != 'delta' or session.context_reset:
        available = set()
    effective = 'delta' if requested == 'delta' and available else 'full'
    reason = ('context_reset' if session.context_reset and requested == 'delta' else
              'no_available_sections_declared' if requested == 'delta' and not available else None)
    warnings = []
    if effective == 'delta':
        warnings.append('Some common context was omitted based on a host declaration; retention is not verified. If a needed section is missing after compaction or a host/model change, request a full summon.')
    core_keys = ('id','display_name','signature','allowed_tools','memory_policy','closeout_rules','aliases','profile_kind')
    packet = {'profile': {k:profile[k] for k in core_keys}, **store._prompt_sections(profile_id),
              'selection': {'profile_id': profile_id, 'settled': True}}
    # Distinctive prompts are always delivered, never abbreviated by delta.
    if profile['profile_kind'] == 'companion':
        packet['soul'] = store.read_soul(profile_id)
    if mode in {'conversation','forum'}:
        booted = store.boot(profile_id)
        packet['compact_state'] = booted['compact_state']
        packet['memories'] = [{'kind':e['kind'],'content':e['content']} for e in booted['recent_memories']]
        packet['recent_exchanges'] = store.recent_exchanges(profile_id,4)
        if mode == 'conversation':
            packet['recent_session_log'] = store.recent_session_log(profile_id,5)
        packet['you_got_mail'] = bool(store.list_inbox(profile_id,True,1))
        if can_read_records:
            packet['data_sources'] = {'profile_stores': [
                {k:item[k] for k in ('name','purpose','status','version','schema')}
                for item in dyn.list(profile_id)]}
    omitted = []
    if 'system_contracts' not in available:
        packet['system_contracts'] = {'companion':companion_contract()} if profile['profile_kind']=='companion' else {}
    else:
        omitted.append('system_contracts')
    # Owner biography and global discovery are relevant to conversation/forum,
    # not automatically inherited by editorial, acting or engineering briefs.
    if mode == 'conversation':
        if 'identity' not in available:
            packet['identity'] = (Path(identity_file).read_text() if can_read_identity and identity_file and Path(identity_file).is_file() else None)
        else:
            omitted.append('identity')
    elif mode == 'forum':
        packet['identity'] = None
    if mode in {'conversation','forum'}:
        if 'companion_directory' not in available:
            packet['companion_directory'] = [
                {'id':p['id'],'display_name':p['display_name'],
                 'signature':store._prompt_sections(p['id'])['signature'] or p['signature'],
                 'lane':store._prompt_sections(p['id'])['lane']}
                for p in store.list_profiles()]
        else:
            omitted.append('companion_directory')
    now = time.time()
    packet['server_time'] = {'unix':now,'iso':datetime.fromtimestamp(now,timezone.utc).isoformat(),
                             'madrid_iso':datetime.fromtimestamp(now,ZoneInfo('Europe/Madrid')).isoformat()}
    task = options.task.model_dump(exclude_none=True) if options.task else {}
    if mode == 'actor' and not task.get('character'):
        warnings.append('No character supplied: request a role and scene before performing.')
    refs = task.get('resource_refs',[]) + task.get('environment',{}).get('diagnostics_refs',[])
    if refs or task.get('environment',{}).get('repo_ref'):
        warnings.append('Resource/repo references are host-supplied labels, not loaded content. Use authorized host tools to obtain missing evidence.')
    packet['invocation'] = {'mode':mode,'requested_mode':options.mode,'policy_version':POLICY_VERSION,
                            'instructions':MODE_INSTRUCTIONS[mode], 'task':task,
                            'permissions':'unchanged; mode, task and session declarations are not grants',
                            'isolation':'narrative_instructions_only' if mode=='actor' else 'existing_permissions',
                            'persistence':'task/scene is ephemeral; no automatic memory, file or canon write'}
    revision_keys = ('profile','who_you_are','voice','what_you_do','lane','signature','how_you_keep_context','soul')
    revision = hashlib.sha256(json.dumps({k:packet[k] for k in revision_keys if k in packet},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    included = sorted(packet)
    packet['hydration'] = {'requested':requested,'effective':effective,'mode':effective,
                           'included_sections':included,'omitted_sections':sorted(omitted),
                           'fallback_to_full':requested=='delta' and effective=='full',
                           'fallback_reason':reason,'profile_revision':revision,
                           'receipt':{'id':uuid.uuid4().hex,'kind':'informational_delivery_record',
                                      'profile_id':profile_id,'mode':mode,'session_id':session.id,
                                      'profile_revision':revision,'delivered_sections':included,
                                      'issued_at':now,'retention_verified':False},
                           'warnings':warnings,
                           'reload':{'profile_id':profile_id,'mode':mode,'hydration':'full',
                                     **({'task':task} if task else {})}}
    return packet
